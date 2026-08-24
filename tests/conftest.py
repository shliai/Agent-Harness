from __future__ import annotations

from collections.abc import AsyncGenerator
from unittest.mock import patch

import pytest

from harness.core.registry import Registry
from harness.domain.models import AgentMessage
from harness.llm.base import AbstractLLMClient, LLMReply
from harness.observability.tracer import Tracer
from harness.tools.base import BaseTool, ToolSpec


class MockLLMClient(AbstractLLMClient):
    def __init__(self, response: str = "测试回复") -> None:
        self.response = response
        self.last_messages: list[AgentMessage] = []
        self.all_calls: list[list[AgentMessage]] = []

    async def chat_async(
        self, messages: list[AgentMessage], temperature: float | None = None
    ) -> LLMReply:
        self.last_messages = messages
        self.all_calls.append(list(messages))
        return LLMReply(content=self.response, total_tokens=len(self.response) // 4)

    async def stream_chat_async(
        self, messages: list[AgentMessage], temperature: float | None = None
    ) -> AsyncGenerator[str, None]:
        self.last_messages = messages
        self.all_calls.append(list(messages))
        yield self.response


class MockTool(BaseTool):
    spec = ToolSpec(
        name="mock_tool",
        description="测试用工具",
        parameters={
            "type": "object",
            "properties": {"input": {"type": "string"}},
            "required": ["input"],
        },
    )

    def __init__(self, response: str = "mock_result") -> None:
        self.response = response

    async def run(self, **kwargs: str) -> str:
        return self.response


@pytest.fixture
def mock_llm() -> MockLLMClient:
    return MockLLMClient()


@pytest.fixture
def tool_registry() -> Registry:
    registry = Registry()
    registry.register_tool(MockTool(response="工具执行成功"))
    return registry


@pytest.fixture
def tracer() -> Tracer:
    return Tracer(enabled=True)


@pytest.fixture
def settings_override() -> Generator[None, None, None]:
    with patch("harness.config.settings") as mock:
        mock.max_iterations = 10
        mock.temperature = 0.7
        mock.tracing_enabled = True
        mock.short_term_window = 100
        yield


@pytest.fixture
def seeded_db(tmp_path, monkeypatch):
    """临时 SQLite 业务库：拟真商品+订单+物流全量入库，返回数据句柄"""
    from types import SimpleNamespace

    from harness.config import settings
    from harness.storage import db as store
    from harness.storage.seeds import DEMO_USER, SECOND_USER, load_logistics, load_orders, load_products

    monkeypatch.setattr(settings, "db_path", tmp_path / "test.db")
    store.init_schema()

    products = load_products()
    with store.db() as c:
        for p in products:
            store.upsert_product(c, p)
    orders, logistics = load_orders(), load_logistics()
    now_iso = "2026-08-23T00:00:00"
    with store.db() as c:
        c.executemany(
            """INSERT INTO orders(order_id,user_id,product_id,product_name,
                                 price,qty,discount_coupon,discount_promo,
                                 status,logistics_no,courier,created_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            [(o["order_id"], o["user_id"], o["product_id"], o["product_name"],
              o["price"], o["qty"], o.get("discount_coupon", 0),
              o.get("discount_promo", 0), o["status"], o.get("logistics_no", ""),
              o.get("courier", ""), o["created_at"]) for o in orders],
        )
        c.executemany(
            """INSERT INTO logistics(tracking_no,nodes_json,updated_at) VALUES(?,?,?)""",
            [(tno, __import__("json").dumps(nodes, ensure_ascii=False), now_iso)
             for tno, nodes in logistics.items()],
        )

    return SimpleNamespace(
        products=products,
        orders=orders,
        logistics=logistics,
        demo_user=DEMO_USER,
        second_user=SECOND_USER,
        demo_orders=[o for o in orders if o["user_id"] == DEMO_USER],
        second_orders=[o for o in orders if o["user_id"] == SECOND_USER],
    )
