from __future__ import annotations

import pytest

from harness.core.agent import Agent
from harness.core.registry import Registry
from harness.domain.models import AgentMessage
from harness.llm.base import AbstractLLMClient
from harness.tools.base import BaseTool, ToolSpec
from tests.conftest import MockLLMClient, MockTool


class FlakyTool(BaseTool):
    """前 n 次失败，之后成功，用于测试重试"""
    spec = ToolSpec(
        name="flaky_tool",
        description="不稳定的测试工具",
        parameters={
            "type": "object",
            "properties": {"input": {"type": "string"}},
            "required": ["input"],
        },
    )

    def __init__(self, fail_count: int = 1) -> None:
        self.calls = 0
        self.fail_count = fail_count

    async def run(self, **kwargs: str) -> str:
        self.calls += 1
        if self.calls <= self.fail_count:
            raise ValueError(f"模拟失败第{self.calls}次")
        return "重试成功"


class StatefulMockLLM(AbstractLLMClient):
    """按顺序返回预设回复：str=最终回答文本；dict=原生结构化工具调用"""

    def __init__(self, responses: list) -> None:
        self.responses = responses
        self.call_count = 0

    def _next_reply(self):
        i = self.call_count
        self.call_count += 1
        return self.responses[i] if i < len(self.responses) else self.responses[-1]

    async def chat_async(
        self, messages: list[AgentMessage], temperature: float | None = None,
        tools: list[dict] | None = None, tool_call_sink: dict | None = None,
        tool_choice: str | None = None,
    ):
        from harness.llm.base import LLMReply

        item = self._next_reply()
        if isinstance(item, dict):
            if tool_call_sink is not None:
                tool_call_sink["tool_calls"] = [item]
            return LLMReply(content="", total_tokens=8, tool_calls=[item])
        return LLMReply(content=item, total_tokens=len(item) // 4)

    async def stream_chat_async(
        self, messages: list[AgentMessage], temperature: float | None = None,
        tools: list[dict] | None = None, tool_call_sink: dict | None = None,
        tool_choice: str | None = None,
    ):
        item = self._next_reply()
        if isinstance(item, dict):
            if tool_call_sink is not None:
                tool_call_sink["tool_calls"] = [item]
            return
        yield item


def _native_call(tool: str, **arguments: str) -> dict:
    return {"id": f"c_{tool}", "name": tool, "arguments": arguments}


class TestAgentLoop:
    @pytest.mark.asyncio
    async def test_tool_retry_success(self) -> None:
        """flaky tool 前1次失败，LLM 修正后第2次成功"""
        flaky = FlakyTool(fail_count=1)
        registry = Registry()
        registry.register_tool(flaky)

        mock_llm = StatefulMockLLM([
            _native_call("flaky_tool", input="x"),
            _native_call("flaky_tool", input="x"),
            "查好了，结果在这里",
        ])
        agent = Agent(llm=mock_llm, registry=registry)
        result = await agent.run("测试")

        assert result.success
        # 第1次失败 → 重试 → 第2次成功
        assert flaky.calls == 2

    @pytest.mark.asyncio
    async def test_tool_retry_exhausted(self) -> None:
        """flaky tool 永远失败，耗尽重试次数后放弃"""
        flaky = FlakyTool(fail_count=99)
        registry = Registry()
        registry.register_tool(flaky)

        mock_llm = StatefulMockLLM([
            _native_call("flaky_tool", input="x"),
            _native_call("flaky_tool", input="x"),
            _native_call("flaky_tool", input="x"),
            "抱歉查不到",
        ])
        agent = Agent(llm=mock_llm, registry=registry)
        result = await agent.run("测试")

        assert not result.steps[0].tool_result.success
        assert flaky.calls == 3  # 初始1次 + 2次重试 = 3

    @pytest.mark.asyncio
    async def test_direct_answer_no_tool_call(self) -> None:
        mock_llm = MockLLMClient(response="你好，有什么可以帮助您的？")
        agent = Agent(llm=mock_llm)
        result = await agent.run("你好")
        assert result.success
        assert "你好" in result.answer

    @pytest.mark.asyncio
    async def test_tool_call_and_response(self) -> None:
        mock_llm = MockLLMClient(tool_calls=[
            {"id": "c1", "name": "mock_tool", "arguments": {"input": "test"}},
        ])
        registry = Registry()
        registry.register_tool(MockTool(response="知识库返回的结果"))

        agent = Agent(llm=mock_llm, registry=registry)
        result = await agent.run("查一下信息")

        assert result.steps[0].tool_call is not None
        assert result.steps[0].tool_call.tool_name == "mock_tool"
        assert result.steps[0].tool_result is not None
        assert result.steps[0].tool_result.success

    @pytest.mark.asyncio
    async def test_session_persistence(self) -> None:
        mock_llm = MockLLMClient(response="回答")
        agent = Agent(llm=mock_llm)

        result1 = await agent.run("第一轮", session_id="test_sess")
        assert result1.success

        result2 = await agent.run("第二轮", session_id="test_sess")
        assert result2.success

        history = agent.conversation_history.load("test_sess")
        assert history is not None
        assert len(history) >= 2

    @pytest.mark.asyncio
    async def test_subtask_dispatch_tool_registered(self) -> None:
        agent = Agent()
        tool = agent.registry.get_tool("subtask_dispatch")
        assert tool is not None
        assert tool.spec.description is not None

    @pytest.mark.asyncio
    async def test_subtask_dispatch_execution(self) -> None:
        mock_llm = StatefulMockLLM([
            _native_call("subtask_dispatch", tasks=[
                {"id": "t1", "description": "查天气", "tools": ["mock_tool"]},
            ]),
            "子任务结果: 今天晴天",
        ])
        registry = Registry()
        registry.register_tool(MockTool(response="天气晴朗，25度"))

        agent = Agent(llm=mock_llm, registry=registry)
        result = await agent.run("帮我查一下")

        assert result.success
        assert "天气" in result.answer or "晴天" in result.answer or "子任务" in result.answer
        assert mock_llm.call_count >= 2

    @pytest.mark.asyncio
    async def test_tracer_records_steps(self) -> None:
        mock_llm = MockLLMClient(response="回答")
        agent = Agent(llm=mock_llm)

        await agent.run("测试")
        trace_log = agent.get_trace_log()
        assert len(trace_log) > 0
