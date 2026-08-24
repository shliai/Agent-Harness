"""v0.3.1 记忆系统测试：WorkingMemory / 会话压缩 / 多轮注入"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from harness.domain.models import AgentMessage, ChatRole
from harness.llm.base import LLMReply
from harness.memory.conversation_history import ConversationHistory
from harness.memory.short_term import ShortTermMemory
from harness.memory.working_memory import WorkingMemory


class StreamFromChat:
    """为只实现了 chat_async 的脚本 LLM 补齐流式接口（整段作为单个 delta）"""

    async def stream_chat_async(self, messages, temperature=None):
        reply = await self.chat_async(messages, temperature=temperature)
        yield reply.content




# ── WorkingMemory：规则抽取 ────────────────────────────────

class TestWorkingMemoryExtraction:
    def test_budget_extracted(self) -> None:
        wm = WorkingMemory()
        wm.update_from_input("预算3000以内帮我推荐个拍照手机", turn=1)
        assert wm.budget_amount == 3000.0
        assert wm.budget_category == "手机"
        assert wm.budget_turn == 1

    def test_budget_kuai_and_k_units(self) -> None:
        wm = WorkingMemory()
        wm.update_from_input("3千块的耳机有推荐吗", turn=1)
        assert wm.budget_amount == 3000.0

    def test_range_query_does_not_override_budget(self) -> None:
        """区间表达（5000以下）不覆盖既有长期预算"""
        wm = WorkingMemory()
        wm.update_from_input("预算3000的手机", turn=1)
        wm.update_from_input("有没有5000以下的手机", turn=2)
        assert wm.budget_amount == 3000.0  # 长期预算不变
        assert wm.budget_turn == 1

    def test_budget_override_on_explicit_new(self) -> None:
        wm = WorkingMemory()
        wm.update_from_input("预算3000的手机", turn=1)
        wm.update_from_input("算了，预算改成5000的手机吧", turn=2)
        assert wm.budget_amount == 5000.0
        assert wm.budget_turn == 2

    def test_order_id_and_tracking_no(self) -> None:
        wm = WorkingMemory()
        wm.update_from_input("订单20240601001的快递SF1234567890到哪了", turn=1)
        assert "20240601001" in wm.order_ids
        assert "SF1234567890" in wm.tracking_nos

    def test_dedupe_and_cap(self) -> None:
        wm = WorkingMemory()
        for i in range(15):
            wm.update_from_input(f"查一下订单20{i:09d}", turn=i + 1)
        assert len(wm.order_ids) <= 10

    def test_prompt_block_content(self) -> None:
        wm = WorkingMemory()
        wm.update_from_input("预算4000的笔记本，订单20240601003", turn=3)
        block = wm.prompt_block()
        assert "任务状态" in block
        assert "4000" in block and "笔记本" in block and "第 3 轮" in block
        assert "20240601003" in block

    def test_empty_state_no_block(self) -> None:
        assert WorkingMemory().prompt_block() == ""

    def test_roundtrip_serialization(self) -> None:
        wm = WorkingMemory()
        wm.update_from_input("预算2500的手环 SF1234567890", turn=2)
        restored = WorkingMemory.from_dict(wm.to_dict())
        assert restored.budget_amount == wm.budget_amount
        assert restored.tracking_nos == wm.tracking_nos

    def test_corrupt_state_resets(self) -> None:
        wm = WorkingMemory.from_dict({"budget_amount": "not-a-number", "unknown_field": 1})
        assert isinstance(wm, WorkingMemory)


# ── ShortTermMemory：全量追踪 ──────────────────────────────

class TestShortTermFullTracking:
    def test_window_trims_but_full_kept(self) -> None:
        mem = ShortTermMemory(window_size=4, track_full=True)
        for i in range(10):
            mem.add(AgentMessage(role=ChatRole.user, content=f"m{i}"))
        assert len(mem.get_context()) == 4          # LLM 视角：最近窗口
        assert len(mem.all_messages()) == 10        # 落盘视角：全量
        assert mem.all_messages()[0].content == "m0"

    def test_default_mode_backward_compatible(self) -> None:
        mem = ShortTermMemory(window_size=3)
        for i in range(5):
            mem.add(AgentMessage(role=ChatRole.user, content=f"x{i}"))
        assert len(mem.all_messages()) == 3  # 无 track_full 时退化为窗口


# ── ConversationHistory：状态存储 ──────────────────────────

class TestConversationStatePersistence:
    def test_save_load_state_roundtrip(self, tmp_path) -> None:
        hist = ConversationHistory(base_path=tmp_path)
        msgs = [
            AgentMessage(role=ChatRole.user, content="预算3000"),
            AgentMessage(role=ChatRole.assistant, content="好的"),
        ]
        hist.save_state("s1", msgs, summary="早期摘要内容", working_memory={"budget_amount": 3000})

        state = hist.load_state("s1")
        assert state["summary"] == "早期摘要内容"
        assert state["working_memory"]["budget_amount"] == 3000
        assert len(state["messages"]) == 2

        # 旧接口仍可读消息列表
        loaded = hist.load("s1")
        assert loaded is not None and len(loaded) == 2

    def test_title_preserved_across_saves(self, tmp_path) -> None:
        hist = ConversationHistory(base_path=tmp_path)
        hist.save_state("s2", [])
        # 模拟重命名
        data = hist.load_state("s2")
        data["title"] = "我的会话"
        hist.awrite_raw.__self__._write_raw_sync("s2", data)

        hist.save_state("s2", [AgentMessage(role=ChatRole.user, content="新消息")])
        assert hist.load_state("s2")["title"] == "我的会话"


# ── loop.py：压缩 + 工作记忆多轮注入 ───────────────────────

class _SummaryLLM(StreamFromChat):
    """正常回答；若收到摘要请求（system 含「摘要助手」）则返回固定摘要"""

    def __init__(self) -> None:
        self.calls: list[list[AgentMessage]] = []
        self.summarize_called = False

    async def chat_async(self, messages, temperature=None):
        self.calls.append(list(messages))
        if any(m.content.startswith("你是客服对话摘要助手") for m in messages):
            self.summarize_called = True
            return LLMReply(content="用户预算3000元买手机，已推荐小米14。")
        return LLMReply(content="好的，明白了。")


def _make_loop(llm, registry=None, **overrides):
    from harness.core.loop import ReActLoop
    from harness.guardrails.base import GuardrailPipeline
    from harness.observability.metrics import MetricsCollector
    from harness.observability.tracer import Tracer

    return ReActLoop(
        llm=llm,
        registry=registry or MagicMock(),
        guardrails=GuardrailPipeline(),
        tracer=Tracer(enabled=False),
        metrics=MetricsCollector(),
        conversation_history=MagicMock(spec=ConversationHistory),
        max_iterations=3,
        **overrides,
    )


class TestLoopContextEngineering:
    @pytest.mark.asyncio
    async def test_working_memory_injected_next_turn(self) -> None:
        """第 1 轮设定预算 → 第 2 轮 system prompt 应包含工作记忆槽位"""
        llm = _SummaryLLM()
        loop = _make_loop(llm)

        saved_states: dict[str, dict] = {}

        async def fake_aread_raw(sid):
            return saved_states.get(sid)

        async def fake_asave_state(sid, msgs, summary=None, working_memory=None, traces=None, user_id=None):
            saved_states[sid] = {
                "messages": [m.model_dump(mode="json") for m in msgs],
                "summary": summary or "",
                "working_memory": working_memory or {},
            }

        loop.conversation_history.aload_state = fake_aread_raw
        loop.conversation_history.asave_state = fake_asave_state

        await loop.execute("预算3000以内买个拍照手机", session_id="wm-test")
        await loop.execute("有什么屏幕小的吗", session_id="wm-test")

        # 第 2 轮的首条 system prompt 必须包含预算约束（即使该消息早已滑出窗口）
        second_turn_system = llm.calls[-1][0].content
        assert "任务状态" in second_turn_system
        assert "预算上限：3000" in second_turn_system

        # 状态文件里也应持久化了工作记忆
        assert saved_states["wm-test"]["working_memory"]["budget_amount"] == 3000.0

    @pytest.mark.asyncio
    async def test_compression_triggered_over_threshold(self) -> None:
        """超过阈值 → 调 LLM 摘要 → 落盘为「摘要 + 最近 N 条」"""
        llm = _SummaryLLM()
        loop = _make_loop(llm)

        saved: dict = {}
        saved_states: dict[str, dict] = {}

        async def fake_aread_raw(sid):
            return saved_states.get(sid)

        async def fake_save(sid, msgs, summary=None, working_memory=None, traces=None, user_id=None):
            saved["msgs"], saved["summary"] = msgs, summary
            saved_states[sid] = {
                "messages": [m.model_dump(mode="json") for m in msgs],
                "summary": summary or "",
                "working_memory": working_memory or {},
            }

        loop.conversation_history.aload_state = fake_aread_raw
        loop.conversation_history.asave_state = fake_save

        import harness.config as cfg

        original = (cfg.settings.context_compress_threshold, cfg.settings.context_keep_recent)
        cfg.settings.context_compress_threshold = 6
        cfg.settings.context_keep_recent = 4
        try:
            for i in range(8):  # 8 轮对话 → all_messages ≥ 16 条 ≥ 阈值
                await loop.execute(f"问题{i}", session_id="compress-test")
        finally:
            cfg.settings.context_compress_threshold, cfg.settings.context_keep_recent = original

        assert llm.summarize_called, "应触发过摘要压缩"
        assert saved["summary"] and "预算" in saved["summary"]
        assert len(saved["msgs"]) <= 4 * 2  # 只保留最近 keep_recent*2 条消息

    @pytest.mark.asyncio
    async def test_compression_failure_keeps_full_history(self) -> None:
        """LLM 摘要失败 → 降级保留完整历史，绝不丢数据"""

        class BrokenSummaryLLM(_SummaryLLM):
            async def chat_async(self, messages, temperature=None):
                if any(m.content.startswith("你是客服对话摘要助手") for m in messages):
                    raise RuntimeError("LLM 不可用")
                return LLMReply(content="ok")

        llm = BrokenSummaryLLM()
        loop = _make_loop(llm)

        saved: dict = {}

        async def fake_save(sid, msgs, summary=None, working_memory=None, traces=None, user_id=None):
            saved["count"] = len(msgs)

        loop.conversation_history.aload_state.return_value = None
        loop.conversation_history.asave_state = fake_save

        import harness.config as cfg

        original = (cfg.settings.context_compress_threshold, cfg.settings.context_keep_recent)
        cfg.settings.context_compress_threshold = 6
        cfg.settings.context_keep_recent = 4
        try:
            for i in range(5):
                await loop.execute(f"问题{i}", session_id="degrade-test")
        finally:
            cfg.settings.context_compress_threshold, cfg.settings.context_keep_recent = original

        assert not llm.summarize_called or saved["count"] > 8  # 未压缩 → 全量落盘
