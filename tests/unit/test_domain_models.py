from __future__ import annotations

from harness.domain.models import AgentMessage, AgentResult, ChatRole, ToolCall


class TestAgentMessage:
    def test_create_user_message(self) -> None:
        msg = AgentMessage(role=ChatRole.user, content="你好")
        assert msg.role == ChatRole.user
        assert msg.content == "你好"
        assert msg.tool_call_id is None

    def test_to_llm_format(self) -> None:
        msg = AgentMessage(role=ChatRole.user, content="你好")
        fmt = msg.to_llm_format()
        assert fmt == {"role": "user", "content": "你好"}

    def test_to_llm_format_with_tool(self) -> None:
        msg = AgentMessage(
            role=ChatRole.tool,
            content="工具返回结果",
            tool_call_id="call_123",
            tool_name="test_tool",
        )
        fmt = msg.to_llm_format()
        assert fmt["tool_call_id"] == "call_123"
        assert fmt["name"] == "test_tool"


class TestAgentResult:
    def test_default_values(self) -> None:
        result = AgentResult(answer="测试回答")
        assert result.answer == "测试回答"
        assert result.steps == []
        assert result.total_duration_ms == 0.0
        assert result.success is True
        assert result.error is None

    def test_error_result(self) -> None:
        result = AgentResult(answer="错误", success=False, error="出错了")
        assert result.success is False
        assert result.error == "出错了"


class TestToolCall:
    def test_create_tool_call(self) -> None:
        tc = ToolCall(tool_name="search", arguments={"q": "hello"})
        assert tc.tool_name == "search"
        assert tc.arguments == {"q": "hello"}
        assert tc.id.startswith("call_")



