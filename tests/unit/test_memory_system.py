"""v0.3.1 记忆系统测试：WorkingMemory / 会话压缩 / 多轮注入"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from harness.domain.models import AgentMessage, ChatRole
from harness.core.loop import ReActLoop
from harness.llm.base import LLMReply
from harness.memory.conversation_history import ConversationHistory
from harness.memory.short_term import ShortTermMemory
from harness.memory.working_memory import WorkingMemory


class StreamFromChat:
    """为只实现了 chat_async 的脚本 LLM 补齐流式接口（整段作为单个 delta）"""

    async def stream_chat_async(self, messages, temperature=None, tools=None, tool_call_sink=None):
        reply = await self.chat_async(messages, temperature=temperature,
                                      tools=tools, tool_call_sink=tool_call_sink)
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
    def test_append_only_full_kept(self) -> None:
        """只追加语义：context 与 full 同步增长，裁剪仅由 trim_to 触发"""
        mem = ShortTermMemory(window_size=4, track_full=True)
        for i in range(10):
            mem.add(AgentMessage(role=ChatRole.user, content=f"m{i}"))
        assert len(mem.get_context()) == 10         # 只追加：不自动淘汰
        assert len(mem.all_messages()) == 10        # 落盘视角：全量
        assert mem.all_messages()[0].content == "m0"

    def test_default_mode_backward_compatible(self) -> None:
        mem = ShortTermMemory(window_size=3)
        for i in range(5):
            mem.add(AgentMessage(role=ChatRole.user, content=f"x{i}"))
        assert len(mem.all_messages()) == 5  # 无 track_full 时退化为当前列表（同样只追加）


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
    """正常回答；若收到摘要请求（system 含档案员提示词）则返回固定摘要"""

    def __init__(self) -> None:
        self.calls: list[list[AgentMessage]] = []
        self.summarize_called = False

    async def chat_async(self, messages, temperature=None, tools=None, tool_call_sink=None):
        self.calls.append(list(messages))
        from harness.core.loop import ReActLoop

        if any(m.content.startswith(ReActLoop.SUMMARY_SYSTEM_PROMPT) for m in messages):
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


class TestChapterMemory:
    """LSM 式章节前缀：压缩 = 摘要+WM快照烘焙成章，追加后冻结，WM清零开新周期"""

    def test_reset_for_new_cycle_clears_knowledge_keeps_counters(self) -> None:
        wm = WorkingMemory(
            budget_amount=3000.0, budget_category="手机", budget_turn=1,
            order_ids=["20240601001"], tracking_nos=["SF1234567890"],
            tokens_used=4567, updated_turn=9,
        )
        wm.set_awaiting("具体型号")

        wm.reset_for_new_cycle()

        assert wm.budget_amount is None
        assert wm.order_ids == [] and wm.tracking_nos == []
        assert wm.awaiting_slot is None
        assert wm.tokens_used == 4567          # 会话级计数器保留
        assert wm.updated_turn == 9

    def test_build_messages_renders_chapters_as_frozen_block(self) -> None:
        """章节渲染为独立 system 消息，位于系统提示词之后、历史之前"""
        from harness.core.loop import ReActLoop

        memory = ShortTermMemory(window_size=10)
        memory.add(AgentMessage(role=ChatRole.user, content="新问题"))
        msgs = ReActLoop._build_messages(
            "人设X", memory,
            chapters=["【第1阶段】压缩A\n【该阶段任务状态】\n预算3000"],
            state_note=None,
        )
        assert len(msgs) == 3                  # [system][chapter1][user]
        assert msgs[1].role == ChatRole.system
        assert "历史记忆章节" in msgs[1].content
        assert "第1阶段" in msgs[1].content and "3000" in msgs[1].content
        # 多章节保持给定顺序（追加语义）
        msgs2 = ReActLoop._build_messages("人设X", memory, chapters=["章一", "章二"])
        assert "章一" in msgs2[1].content and "章二" in msgs2[2].content

    @pytest.mark.asyncio
    async def test_loop_persists_chapters_and_resets_wm(self) -> None:
        """压缩事件后：chapters 追加新章、WM 清零、消息裁剪"""
        llm = _SummaryLLM()
        loop = _make_loop(llm)
        saved_states: dict[str, dict] = {}

        async def fake_aread_raw(sid):
            return saved_states.get(sid)

        async def fake_asave_state(sid, msgs, summary=None, working_memory=None,
                                   traces=None, user_id=None, chapters=None):
            saved_states[sid] = {
                "messages": [m.model_dump(mode="json") for m in msgs],
                "summary": summary or "",
                "working_memory": working_memory or {},
                "chapters": chapters if chapters is not None else [],
            }

        loop.conversation_history.aload_state = fake_aread_raw
        loop.conversation_history.asave_state = fake_asave_state

        import harness.config as cfg
        original = (cfg.settings.context_window_tokens, cfg.settings.context_compress_ratio,
                    cfg.settings.context_keep_recent)
        # 阈值 = 30*0.5=15 token：1 轮(≈9)不触发，2 轮(≈16)触发压缩 → 恰产生 1 个章节
        cfg.settings.context_window_tokens = 30
        cfg.settings.context_compress_ratio = 0.5
        cfg.settings.context_keep_recent = 2
        try:
            await loop.execute("预算3000买个手机", session_id="bake-test")
            await loop.execute("还有别的吗", session_id="bake-test")
        finally:
            (cfg.settings.context_window_tokens, cfg.settings.context_compress_ratio,
             cfg.settings.context_keep_recent) = original

        state = saved_states["bake-test"]
        assert len(state["chapters"]) == 1, "应产生第1章节"
        assert "3000" in state["chapters"][0]
        assert "任务状态" in state["chapters"][0]
        # WM 已清零（预算随快照进入章节，槽位归空）
        assert state["working_memory"].get("budget_amount") is None
        assert len(state["messages"]) <= 2 * 2



    @pytest.mark.asyncio
    async def test_working_memory_isolated_per_session(self) -> None:
        """工作记忆按会话隔离：不同 session 的预算/事实互不可见"""
        llm = _SummaryLLM()
        loop = _make_loop(llm)

        saved_states: dict[str, dict] = {}

        async def fake_aread_raw(sid):
            return saved_states.get(sid)

        async def fake_asave_state(sid, msgs, summary=None, working_memory=None, traces=None, user_id=None, chapters=None):
            saved_states[sid] = {
                "messages": [m.model_dump(mode="json") for m in msgs],
                "summary": summary or "",
                "working_memory": working_memory or {},
            }

        loop.conversation_history.aload_state = fake_aread_raw
        loop.conversation_history.asave_state = fake_asave_state

        await loop.execute("预算3000买个手机", session_id="sess-A")
        await loop.execute("预算8000买个笔记本", session_id="sess-B")

        wm_a = saved_states["sess-A"]["working_memory"]
        wm_b = saved_states["sess-B"]["working_memory"]
        assert wm_a["budget_amount"] == 3000.0
        assert wm_b["budget_amount"] == 8000.0
        # 会话 B 的状态尾注不得出现 A 的预算（隔离）
        turn_b_msgs = llm.calls[-1]
        b_note = next(m for m in turn_b_msgs if m.role == ChatRole.system and m.content.startswith("## 当前任务状态"))
        assert "8000" in b_note.content and "3000" not in b_note.content

    def test_extract_preferences(self) -> None:
        """显式偏好标记 + 品牌/品类识别（确定性）"""
        from harness.memory.working_memory import extract_user_prefs as extract_preferences
        assert extract_preferences("我喜欢索尼耳机") == [("category", "品类=耳机"), ("brand", "品牌=索尼")]
        assert extract_preferences("我偏好买华为手机") == [
            ("category", "品类=手机"), ("brand", "品牌=华为")]
        assert extract_preferences("今天天气不错") == []

    def test_extract_constraints(self) -> None:
        from harness.memory.working_memory import extract_user_constraints as extract_constraints
        assert extract_constraints("我对镍过敏") == [("allergy", "对镍过敏")]
        assert extract_constraints("请避开真皮材质") == [("material", "避开真皮材质")]
        assert extract_constraints("随便看看") == []

    def test_extract_correction(self) -> None:
        from harness.memory.working_memory import extract_user_corrections as extract_correction
        assert extract_correction("不是华为，是苹果") == ("brand", "品牌=苹果")
        assert extract_correction("我要的是索尼") == ("brand", "品牌=索尼")
        assert extract_correction("你好") is None

    def test_trivial_input_skips_learning(self) -> None:
        """寒暄输入不触发学习捕获"""
        from harness.core.loop import _TRIVIAL_INPUT_RE
        assert _TRIVIAL_INPUT_RE.match("你好")
        assert not _TRIVIAL_INPUT_RE.match("我想买个拍照好的手机")



    @pytest.mark.asyncio
    async def test_working_memory_injected_next_turn(self) -> None:
        """第 1 轮设定预算 → 第 2 轮 system prompt 应包含工作记忆槽位"""
        llm = _SummaryLLM()
        loop = _make_loop(llm)

        saved_states: dict[str, dict] = {}

        async def fake_aread_raw(sid):
            return saved_states.get(sid)

        async def fake_asave_state(sid, msgs, summary=None, working_memory=None, traces=None, user_id=None, chapters=None):
            saved_states[sid] = {
                "messages": [m.model_dump(mode="json") for m in msgs],
                "summary": summary or "",
                "working_memory": working_memory or {},
            }

        loop.conversation_history.aload_state = fake_aread_raw
        loop.conversation_history.asave_state = fake_asave_state

        await loop.execute("预算3000以内买个拍照手机", session_id="wm-test")
        await loop.execute("有什么屏幕小的吗", session_id="wm-test")

        # 第 2 轮应包含工作记忆「状态尾注」消息（尾部注入，不破坏前缀缓存）
        turn2_msgs = llm.calls[-1]
        wm_notes = [m for m in turn2_msgs if m.role == ChatRole.system and m.content.startswith("## 当前任务状态")]
        assert wm_notes, "第 2 轮应包含工作记忆尾注消息"
        assert "预算上限：3000" in wm_notes[0].content
        # 尾注位于最后一条用户消息之前
        assert turn2_msgs.index(wm_notes[0]) == len(turn2_msgs) - 2
        # 首条 system prompt 不再携带每轮变化的工作记忆（前缀稳定性）
        assert not turn2_msgs[0].content.startswith("## 当前任务状态")

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

        async def fake_save(sid, msgs, summary=None, working_memory=None, traces=None, user_id=None, chapters=None):
            saved["msgs"], saved["chapters"] = msgs, (chapters if chapters is not None else [])
            saved_states[sid] = {
                "messages": [m.model_dump(mode="json") for m in msgs],
                "summary": summary or "",
                "working_memory": working_memory or {},
                "chapters": chapters if chapters is not None else [],
            }

        loop.conversation_history.aload_state = fake_aread_raw
        loop.conversation_history.asave_state = fake_save

        import harness.config as cfg

        original = (cfg.settings.context_window_tokens, cfg.settings.context_compress_ratio,
                    cfg.settings.context_keep_recent)
        # 阈值 = 100*0.1=10 token：多轮对话累计远超 → 稳定触发压缩
        cfg.settings.context_window_tokens = 100
        cfg.settings.context_compress_ratio = 0.1
        cfg.settings.context_keep_recent = 4
        try:
            for i in range(8):  # 8 轮对话 → token 累计远超阈值
                await loop.execute(f"问题{i}", session_id="compress-test")
        finally:
            (cfg.settings.context_window_tokens, cfg.settings.context_compress_ratio,
             cfg.settings.context_keep_recent) = original

        assert llm.summarize_called, "应触发过摘要压缩"
        assert saved["chapters"], "应产生冻结章节"
        assert any("预算" in ch or "状态" in ch for ch in saved["chapters"])
        assert len(saved["msgs"]) <= 4 * 2  # 只保留最近 keep_recent*2 条消息

    @pytest.mark.asyncio
    async def test_compression_failure_keeps_full_history(self) -> None:
        """LLM 摘要失败 → 降级保留完整历史，绝不丢数据"""

        class BrokenSummaryLLM(_SummaryLLM):
            async def chat_async(self, messages, temperature=None, tools=None, tool_call_sink=None):
                from harness.core.loop import ReActLoop

                if any(m.content.startswith(ReActLoop.SUMMARY_SYSTEM_PROMPT) for m in messages):
                    raise RuntimeError("LLM 不可用")
                return LLMReply(content="ok")

        llm = BrokenSummaryLLM()
        loop = _make_loop(llm)

        saved: dict = {}

        async def fake_save(sid, msgs, summary=None, working_memory=None, traces=None, user_id=None, chapters=None):
            saved["count"] = len(msgs)

        loop.conversation_history.aload_state.return_value = None
        loop.conversation_history.asave_state = fake_save

        import harness.config as cfg

        original = (cfg.settings.context_window_tokens, cfg.settings.context_compress_ratio,
                    cfg.settings.context_keep_recent)
        # 阈值 = 100*0.1=10 token：5 轮累计远超 → 触发压缩但 LLM 失败 → 降级保留全量
        cfg.settings.context_window_tokens = 100
        cfg.settings.context_compress_ratio = 0.1
        cfg.settings.context_keep_recent = 4
        try:
            for i in range(5):
                await loop.execute(f"问题{i}", session_id="degrade-test")
        finally:
            (cfg.settings.context_window_tokens, cfg.settings.context_compress_ratio,
             cfg.settings.context_keep_recent) = original

        assert not llm.summarize_called or saved["count"] > 8  # 未压缩 → 全量落盘


# ── v0.8.0 上下文工程：滚动摘要 / 归档裁剪 / 记忆层级 ──────

class _FoldAwareLLM(StreamFromChat):
    """chat_async 同时识别章节摘要与折叠摘要两种 system 提示词"""

    def __init__(self) -> None:
        self.fold_calls: list[list[AgentMessage]] = []
        self.fold_called = False

    async def chat_async(self, messages, temperature=None, tools=None, tool_call_sink=None):
        from harness.core.loop import FOLD_SYSTEM_PROMPT, ReActLoop

        if any(m.content.startswith(ReActLoop.SUMMARY_SYSTEM_PROMPT) for m in messages):
            return LLMReply(content="阶段摘要")
        if any(m.content.startswith(FOLD_SYSTEM_PROMPT) for m in messages):
            self.fold_calls.append(list(messages))
            self.fold_called = True
            return LLMReply(content="【诉求】买手机【进展】已推荐【未决】待用户选择")
        return LLMReply(content="好的，已了解您的需求，为您找到了几款合适的产品。")


class _LongAnswerLLM(_SummaryLLM):
    """非摘要请求时输出超过折叠门控长度阈值的正常回答"""

    async def chat_async(self, messages, temperature=None, tools=None, tool_call_sink=None):
        from harness.core.loop import ReActLoop

        if any(m.content.startswith(ReActLoop.SUMMARY_SYSTEM_PROMPT) for m in messages):
            return await super().chat_async(messages, temperature=temperature)
        return LLMReply(content="好的，已根据预算3000元为您筛选出三款拍照优秀的手机，附对比与推荐理由。")


class TestRollingSummary:
    """轮末折叠式滚动摘要：cheap_llm 折叠「旧进展+本轮问答」，失败沿用旧值"""

    @pytest.mark.asyncio
    async def test_first_turn_folds_and_persists(self) -> None:
        llm = _LongAnswerLLM()
        cheap = _FoldAwareLLM()
        loop = _make_loop(llm, cheap_llm=cheap)
        saved: dict = {}

        async def fake_aread_raw(sid):
            return None

        async def fake_asave(sid, msgs, summary=None, working_memory=None,
                             traces=None, user_id=None, chapters=None):
            saved["wm"] = working_memory or {}

        loop.conversation_history.aload_state = fake_aread_raw
        loop.conversation_history.asave_state = fake_asave

        await loop.execute("预算3000帮我推荐一个拍照好的手机，最好说说优缺点",
                           session_id="fold-1")
        assert cheap.fold_called, "有实质内容的首轮应调用折叠"
        assert "【诉求】" in saved["wm"].get("rolling_summary", "")

    @pytest.mark.asyncio
    async def test_second_fold_carries_previous_summary(self) -> None:
        llm = _LongAnswerLLM()
        cheap = _FoldAwareLLM()
        loop = _make_loop(llm, cheap_llm=cheap)
        states: dict[str, dict] = {}

        async def fake_aread_raw(sid):
            return states.get(sid)

        async def fake_asave(sid, msgs, summary=None, working_memory=None,
                             traces=None, user_id=None, chapters=None):
            states[sid] = {"working_memory": working_memory or {}}

        loop.conversation_history.aload_state = fake_aread_raw
        loop.conversation_history.asave_state = fake_asave

        await loop.execute("预算3000帮我推荐一款拍照好的手机并对比参数", session_id="fold-2")
        old_cheap_calls = len(cheap.fold_calls)
        await loop.execute("那第二款的续航怎么样？再详细讲讲", session_id="fold-2")
        assert len(cheap.fold_calls) == old_cheap_calls + 1
        fold_user_msg = cheap.fold_calls[-1][1].content
        assert "【旧进展】【诉求】买手机" in fold_user_msg, "第二轮折叠应携带旧进展"

    @pytest.mark.asyncio
    async def test_trivial_input_skips_fold(self) -> None:
        llm = _SummaryLLM()
        cheap = _FoldAwareLLM()
        loop = _make_loop(llm, cheap_llm=cheap)

        async def fake_aread_raw(sid):
            return None

        async def fake_asave(sid, msgs, summary=None, working_memory=None,
                             traces=None, user_id=None, chapters=None):
            pass

        loop.conversation_history.aload_state = fake_aread_raw
        loop.conversation_history.asave_state = fake_asave

        await loop.execute("你好", session_id="fold-3")
        assert not cheap.fold_called, "寒暄输入不调折叠"

    @pytest.mark.asyncio
    async def test_fold_failure_keeps_old_summary(self) -> None:
        class _BrokenCheap(StreamFromChat):
            async def chat_async(self, messages, **kwargs):
                raise RuntimeError("cheap down")

        llm = _LongAnswerLLM()
        cheap = _BrokenCheap()
        loop = _make_loop(llm, cheap_llm=cheap)
        states: dict[str, dict] = {
            "fold-4": {"working_memory": {"rolling_summary": "旧的可靠进展"}}
        }

        async def fake_aread_raw(sid):
            return states.get(sid)

        async def fake_asave(sid, msgs, summary=None, working_memory=None,
                             traces=None, user_id=None, chapters=None):
            states[sid] = {"working_memory": working_memory or {}}

        loop.conversation_history.aload_state = fake_aread_raw
        loop.conversation_history.asave_state = fake_asave

        await loop.execute("预算3000帮我推荐一款拍照好的手机并详细对比", session_id="fold-4")
        assert states["fold-4"]["working_memory"].get("rolling_summary") == "旧的可靠进展"


class TestArchiveTrimming:
    """归档裁剪：烘焙快照只含硬实体槽位；recent_topics 已删除"""

    def test_recent_topics_removed(self) -> None:
        wm = WorkingMemory()
        assert not hasattr(wm, "recent_topics")

    def test_archive_block_excludes_transients(self) -> None:
        wm = WorkingMemory(budget_amount=3000.0)
        wm.set_awaiting("订单号")
        wm.rolling_summary = "进行中的叙事"

        live = wm.prompt_block()
        archive = wm.prompt_block(for_archive=True)

        assert "对话进展（摘要）" in live and "等待用户提供" in live
        assert "3000" in archive
        assert "对话进展" not in archive and "等待用户提供" not in archive


class TestMemoryHierarchyPrompt:
    """记忆层级交待：提示词教模型读章节；章节前缀带 N/M"""

    def test_system_prompt_contains_hierarchy_rules(self) -> None:
        from harness.core.loop import SYSTEM_PROMPT_TEMPLATE
        assert "记忆层级" in SYSTEM_PROMPT_TEMPLATE
        assert "第N阶段" in SYSTEM_PROMPT_TEMPLATE
        assert "越近的章节越优先" in SYSTEM_PROMPT_TEMPLATE

    def test_chapter_prefix_numbered(self) -> None:
        memory = ShortTermMemory()
        memory.add(AgentMessage(role=ChatRole.user, content="最新"))
        msgs = ReActLoop._build_messages(
            "人设X", memory, chapters=["章一内容", "章二内容"])
        chapter_msgs = [m for m in msgs if "历史记忆章节" in m.content]
        assert len(chapter_msgs) == 2
        assert "第1/2段" in chapter_msgs[0].content
        assert "第2/2段" in chapter_msgs[1].content
