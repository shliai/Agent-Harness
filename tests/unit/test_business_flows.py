"""业务流测试 v0.4：SQLite 拟真数据上的归属校验 / 枚举风控 / 政策库 / 转人工 / 售后"""
from __future__ import annotations

import asyncio
import json
from unittest.mock import MagicMock

import pytest

from harness.llm.base import LLMReply
from harness.memory.conversation_history import ConversationHistory


class StreamFromChat:
    """为只实现了 chat_async 的脚本 LLM 补齐流式接口（整段作为单个 delta）"""

    async def stream_chat_async(self, messages, temperature=None):
        reply = await self.chat_async(messages, temperature=temperature)
        yield reply.content




# ── 订单归属校验（拟真数据 + 临时 SQLite）──────────────────

class TestOrderOwnership:
    def test_generated_data_covers_two_users(self, seeded_db) -> None:
        users = {o["user_id"] for o in seeded_db.orders}
        assert seeded_db.demo_user in users and seeded_db.second_user in users
        demo_status = {o["status"] for o in seeded_db.demo_orders}
        assert {"待发货", "已发货", "配送中", "已完成"} <= demo_status

    @pytest.mark.asyncio
    async def test_own_order_allowed(self, seeded_db) -> None:
        from harness.tools.context import current_user_id
        from harness.tools.order_query import OrderQueryTool

        own = seeded_db.demo_orders[0]
        current_user_id.set(seeded_db.demo_user)
        out = await OrderQueryTool().run(order_id=own["order_id"])
        assert "不属于" not in out
        assert own["order_id"] in out and own["product_name"] in out

    @pytest.mark.asyncio
    async def test_foreign_order_denied(self, seeded_db) -> None:
        from harness.tools.context import current_user_id
        from harness.tools.order_query import OrderQueryTool

        foreign = seeded_db.second_orders[0]
        current_user_id.set(seeded_db.demo_user)
        out = await OrderQueryTool().run(order_id=foreign["order_id"])
        assert "不属于当前账户" in out

    @pytest.mark.asyncio
    async def test_order_list_filtered_by_user(self, seeded_db) -> None:
        from harness.tools.context import current_user_id
        from harness.tools.order_query import MyOrdersTool

        current_user_id.set(seeded_db.second_user)
        out = await MyOrdersTool().run()
        second_ids = {o["order_id"] for o in seeded_db.second_orders}
        listed = [
            line.split("|")[0].strip("- ").strip()
            for line in out.splitlines() if line.startswith("- ")
        ]
        assert listed
        assert set(listed) <= second_ids

    @pytest.mark.asyncio
    async def test_logistics_from_db(self, seeded_db) -> None:
        from harness.tools.context import current_session_id
        from harness.tools.logistics_query import LogisticsQueryTool

        tno = next(t for t, nodes in seeded_db.logistics.items() if len(nodes) >= 3)
        current_session_id.set("logi-sess")
        out = await LogisticsQueryTool().run(logistics_no=tno)
        assert tno in out and "详细轨迹" in out

        miss = await LogisticsQueryTool().run(logistics_no="ZZ0000000000")
        assert "未找到" in miss or "格式不正确" in miss


# ── 枚举风控 ───────────────────────────────────────────────

class TestEnumerationGuard:
    @pytest.mark.asyncio
    async def test_lockout_after_consecutive_misses(self) -> None:
        from harness.tools.context import EnumerationGuard

        g = EnumerationGuard(max_misses=3, window_seconds=900, cooldown_seconds=1800)
        key = "test:enum-lockout"
        assert g.check(key) is None
        for _ in range(3):
            g.record_miss(key)
        blocked = g.check(key)
        assert blocked is not None and "暂停" in blocked

    @pytest.mark.asyncio
    async def test_hit_resets_counter(self) -> None:
        from harness.tools.context import EnumerationGuard

        g = EnumerationGuard(max_misses=3, window_seconds=900, cooldown_seconds=1800)
        key = "test:enum-reset"
        g.record_miss(key)
        g.record_miss(key)
        g.record_hit(key)
        g.record_miss(key)
        g.record_miss(key)
        assert g.check(key) is None

    @pytest.mark.asyncio
    async def test_order_tool_lockout(self, seeded_db) -> None:
        from harness.tools.context import current_session_id
        from harness.tools.order_query import OrderQueryTool

        current_session_id.set("sess-lockout-test")
        tool = OrderQueryTool()
        key = "order_query:sess-lockout-test"

        tool._guard.record_hit(key)
        for i in range(8):
            await tool.run(order_id=f"99990{i:04d}")
        out = await tool.run(order_id="99999999")
        assert "暂停" in out


# ── 政策库工具 ─────────────────────────────────────────────

class TestPolicyTool:
    @pytest.mark.asyncio
    async def test_refund_policy_hit(self) -> None:
        from harness.tools.policy_query import PolicyQueryTool

        out = await PolicyQueryTool().run(topic="激活过的耳机还能七天无理由退货吗")
        assert "七天无理由" in out or "无理由退货" in out
        assert "不得扩展或编造" in out

    @pytest.mark.asyncio
    async def test_unknown_policy_no_hallucination(self) -> None:
        from harness.tools.policy_query import PolicyQueryTool

        out = await PolicyQueryTool().run(topic="附近有什么好吃的火锅店推荐")
        assert "未在官方政策库" in out or "转人工" in out


# ── 转人工工单 ─────────────────────────────────────────────

class TestTransferHuman:
    @pytest.mark.asyncio
    async def test_ticket_created(self, tmp_path, monkeypatch) -> None:
        from harness.config import settings
        from harness.tools.context import current_session_id, current_user_id
        from harness.tools.policy_query import TransferHumanTool

        monkeypatch.setattr(settings, "data_dir", tmp_path)
        current_user_id.set("demo_user")
        current_session_id.set("t-session")

        out = await TransferHumanTool().run(reason="用户要求超期退货", order_id="X123")
        assert "TK" in out and "工单号" in out and "X123" in out

        files = list((tmp_path / "tickets").glob("tickets_*.jsonl"))
        rec = json.loads(files[0].read_text(encoding="utf-8").strip())
        assert rec["reason"] == "用户要求超期退货"


# ── 售后申请与查询（SQLite 状态机）─────────────────────────

class TestAfterSale:
    @pytest.fixture
    def aftersale_env(self, seeded_db, monkeypatch):
        from harness.tools.context import current_user_id

        current_user_id.set(seeded_db.demo_user)
        return seeded_db

    @pytest.mark.asyncio
    async def test_apply_happy_path_and_idempotent(self, aftersale_env) -> None:
        db = aftersale_env
        own = next(o for o in db.demo_orders if o["status"] in ("已发货", "配送中", "已完成"))
        from harness.tools.aftersale import AfterSaleApplyTool, AfterSaleQueryTool

        out = await AfterSaleApplyTool().run(order_id=own["order_id"], type="退货", reason="不想要了")
        assert "售后单号：AS" in out and "待审核" in out

        out2 = await AfterSaleApplyTool().run(order_id=own["order_id"], type="退货", reason="再试")
        assert "无需重复提交" in out2

        listing = await AfterSaleQueryTool().run()
        assert own["order_id"] in listing and "退货" in listing

    @pytest.mark.asyncio
    async def test_apply_foreign_denied(self, aftersale_env) -> None:
        from harness.tools.aftersale import AfterSaleApplyTool
        from harness.tools.context import current_user_id

        foreign = aftersale_env.second_orders[0]
        current_user_id.set(aftersale_env.demo_user)
        out = await AfterSaleApplyTool().run(order_id=foreign["order_id"], type="换货", reason="x")
        assert "不属于当前账户" in out

    @pytest.mark.asyncio
    async def test_apply_ineligible_status(self, aftersale_env) -> None:
        from harness.tools.aftersale import AfterSaleApplyTool

        pending = next(o for o in aftersale_env.demo_orders if o["status"] == "待发货")
        out = await AfterSaleApplyTool().run(order_id=pending["order_id"], type="退货", reason="x")
        assert "暂不可申请售后" in out and "待发货" in out

    def test_state_machine_rejects_illegal_jump(self) -> None:
        from harness.tools.aftersale import transition

        rec = {"status": "待审核", "updated_at": "", "history": []}
        with pytest.raises(ValueError):
            transition(rec, "已完成")
        transition(rec, "已通过", operator="admin")
        transition(rec, "已完成", operator="admin")
        assert len(rec["history"]) == 2


# ── loop：取消落盘 + 轨迹持久化 ────────────────────────────

class _HangLLM(StreamFromChat):
    async def chat_async(self, messages, temperature=None):
        await asyncio.Event().wait()


class _ToolCallLLM(StreamFromChat):
    def __init__(self) -> None:
        self.calls = 0

    async def chat_async(self, messages, temperature=None):
        self.calls += 1
        if self.calls == 1:
            return LLMReply(
                content='THOUGHT: 查询\nACTION: {"tool": "mock_tool", "arguments": {"input": "x"}}'
            )
        return LLMReply(content="最终回答")


def _make_loop(llm):
    from harness.core.loop import ReActLoop
    from harness.core.registry import Registry
    from harness.guardrails.base import GuardrailPipeline
    from harness.observability.metrics import MetricsCollector
    from harness.observability.tracer import Tracer
    from tests.conftest import MockTool

    reg = Registry()
    reg.register_tool(MockTool(response="工具结果"))
    return ReActLoop(
        llm=llm,
        registry=reg,
        guardrails=GuardrailPipeline(),
        tracer=Tracer(enabled=False),
        metrics=MetricsCollector(),
        conversation_history=MagicMock(spec=ConversationHistory),
        max_iterations=4,
    )


class TestCancelPersistenceAndTraces:
    @pytest.mark.asyncio
    async def test_cancel_saves_partial_state(self) -> None:
        loop = _make_loop(_HangLLM())
        saved: dict = {}

        async def fake_save(sid, msgs, summary=None, working_memory=None, traces=None, user_id=None, chapters=None):
            saved["count"] = len(msgs)

        loop.conversation_history.aload_state.return_value = None
        loop.conversation_history.asave_state = fake_save

        task = asyncio.create_task(loop.execute("帮我查个东西", session_id="cancel-sess"))
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert saved["count"] >= 1

    @pytest.mark.asyncio
    async def test_traces_persisted_on_success(self) -> None:
        loop = _make_loop(_ToolCallLLM())
        saved: dict = {}

        async def fake_save(sid, msgs, summary=None, working_memory=None, traces=None, user_id=None, chapters=None):
            saved["traces"] = traces

        loop.conversation_history.aload_state.return_value = None
        loop.conversation_history.asave_state = fake_save

        result = await loop.execute("查一下", session_id="trace-sess")
        assert result.success
        entry = saved["traces"][-1]
        assert entry["steps"][0]["tool_call"]["tool_name"] == "mock_tool"
