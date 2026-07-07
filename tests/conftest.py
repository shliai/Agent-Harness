from __future__ import annotations

from collections.abc import AsyncGenerator, Generator
from unittest.mock import MagicMock, patch

import pytest

from harness.core.registry import Registry
from harness.domain.models import AgentMessage
from harness.llm.base import AbstractLLMClient
from harness.observability.tracer import Tracer
from harness.tools.base import BaseTool, ToolSpec


class MockLLMClient(AbstractLLMClient):
    def __init__(self, response: str = "测试回复") -> None:
        self.response = response
        self.last_messages: list[AgentMessage] = []
        self.last_token_usage: int = 0

    def chat(self, messages: list[AgentMessage], temperature: float | None = None) -> str:
        self.last_messages = messages
        self.last_token_usage = len(self.response) // 4
        return self.response

    async def chat_async(self, messages: list[AgentMessage], temperature: float | None = None) -> str:
        self.last_messages = messages
        self.last_token_usage = len(self.response) // 4
        return self.response

    def stream_chat(self, messages: list[AgentMessage], temperature: float | None = None) -> Generator[str, None, None]:
        yield self.response

    async def stream_chat_async(self, messages: list[AgentMessage], temperature: float | None = None) -> AsyncGenerator[str, None]:
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
        mock.short_term_window = 20
        yield
