"""v0.3.2 补充：售后写操作 / 状态机 / 输出合规 / 澄清标记"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from harness.llm.base import LLMReply
from harness.memory.working_memory import WorkingMemory


class StreamFromChat:
    """为只实现了 chat_async 的脚本 LLM 补齐流式接口（整段作为单个 delta）"""

    async def stream_chat_async(self, messages, temperature=None):
        reply = await self.chat_async(messages, temperature=temperature)
        yield reply.content




# ── 输出合规护栏 ───────────────────────────────────────────

class TestComplianceFilter:
    def test_promise_softened_with_note(self) -> None:
        from harness.guardrails.compliance_filter import ComplianceFilter

        f = ComplianceFilter()
        out = f.check({"type": "output", "content": "您放心，这个绝对能退，我们保证退款到账。"})
        assert out is not None
        assert "以官方政策条款与人工审核结果为准" in out

    def test_normal_answer_untouched(self) -> None:
        from harness.guardrails.compliance_filter import ComplianceFilter

        f = ComplianceFilter()
        out = f.check({"type": "output", "content": "根据七天无理由退货政策，未激活商品可申请退货。"})
        assert out is None  # 无命中返回 None，不改写

    def test_banned_word_replaced(self) -> None:
        from harness.guardrails.compliance_filter import ComplianceFilter

        f = ComplianceFilter()
        out = f.check({"type": "output", "content": "你去死吧"})
        assert out is not None and "去死" not in out and "**" in out

    def test_input_type_ignored(self) -> None:
        from harness.guardrails.compliance_filter import ComplianceFilter

        f = ComplianceFilter()
        assert f.check({"type": "input", "content": "保证能退"}) is None


# ── 澄清式多轮 ─────────────────────────────────────────────

class TestClarification:
    def test_awaiting_slot_lifecycle(self) -> None:
        wm = WorkingMemory()
        wm.update_from_input("查一下物流", turn=1)

        # 模拟循环检测到反问 → 记录等待项
        wm.set_awaiting("物流单号")
        block = wm.prompt_block()
        assert "等待用户提供" in block and "物流单号" in block

        # 用户回复 → 自动清除
        wm.update_from_input("单号是SF1234567890", turn=2)
        assert wm.awaiting_slot is None
        assert "SF1234567890" in wm.tracking_nos

    @pytest.mark.asyncio
    async def test_loop_detects_clarify_question(self) -> None:
        """最终回答含「请提供订单号」→ WM 应记录等待项并随状态持久化"""
        import re as _re

        from harness.core.loop import ReActLoop
        from harness.guardrails.base import GuardrailPipeline
        from harness.memory.conversation_history import ConversationHistory
        from harness.observability.metrics import MetricsCollector
        from harness.observability.tracer import Tracer

        class AskLLM(StreamFromChat):
            async def chat_async(self, messages, temperature=None):
                return LLMReply(content="请问您要查询哪个订单呢？麻烦提供一下订单号。")

        loop = ReActLoop(
            llm=AskLLM(),
            registry=MagicMock(),
            guardrails=GuardrailPipeline(),
            tracer=Tracer(enabled=False),
            metrics=MetricsCollector(),
            conversation_history=MagicMock(spec=ConversationHistory),
            max_iterations=3,
        )
        loop.conversation_history.aload_state.return_value = None

        saved: dict = {}

        async def fake_save(sid, msgs, summary=None, working_memory=None, traces=None, user_id=None, chapters=None):
            saved["wm"] = working_memory

        loop.conversation_history.asave_state = fake_save

        result = await loop.execute("帮我查物流", session_id="clarify-sess")
        assert result.success
        assert saved["wm"]["awaiting_slot"], "澄清反问应写入 awaiting_slot"

        # 验证 _CLARIFY_RE 确实能命中该话术
        from harness.core.loop import _CLARIFY_RE

        assert _CLARIFY_RE.search(result.answer)
