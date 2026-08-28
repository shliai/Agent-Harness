"""原生 function calling 工具系统的单元测试（文本协议已移除）

覆盖路径：
1. Registry → OpenAI tools schema 载荷生成
2. 结构化 tool_calls 直接构造 ToolCall 并执行（不经过任何文本解析）
3. 工具结果回灌下一次 LLM 调用的上下文
4. 无工具调用 → 视为最终回答
5. 端点不支持 tools → 抛出明确 LLMError（不再静默降级）
6. 坏参数（必填缺失）→ 走失败修正重试链路
"""
from __future__ import annotations

import pytest

from harness.core.loop import ReActLoop
from harness.core.registry import Registry
from harness.domain.exceptions import LLMError
from harness.domain.models import AgentResult
from harness.llm.base import AbstractLLMClient, LLMReply


class FakeLLM(AbstractLLMClient):
    """可编程 Fake：脚本项 str=最终回答；dict=原生结构化工具调用；
    Exception=抛出异常。记录每次调用的 kwargs 供断言。"""

    def __init__(self, script: list) -> None:
        self.script = list(script)
        self.idx = 0
        self.calls: list[dict] = []

    async def chat_async(self, messages, temperature=None, tools=None,
                          tool_call_sink=None, **kwargs):
        self.calls.append({"tools": tools, "messages": messages})
        item = self._next()
        if isinstance(item, Exception):
            raise item
        calls = self._to_calls(item)
        if tool_call_sink is not None and calls:
            tool_call_sink["tool_calls"] = calls
        return LLMReply(
            content="" if calls else str(item),
            total_tokens=10,
            tool_calls=calls,
        )

    async def stream_chat_async(self, messages, temperature=None, tools=None,
                                 tool_call_sink=None, **kwargs):
        self.calls.append({"tools": tools, "messages": messages})
        item = self._next()
        if isinstance(item, Exception):
            raise item
        calls = self._to_calls(item)
        if tool_call_sink is not None and calls:
            tool_call_sink["tool_calls"] = calls
        yield "" if calls else str(item)

    @staticmethod
    def _to_calls(item):
        if isinstance(item, dict):
            return [item]
        if isinstance(item, list):
            return item
        return []

    def _next(self):
        if self.idx >= len(self.script):
            return "好的"
        item = self.script[self.idx]
        self.idx += 1
        return item


class EchoTool:
    class spec:  # noqa: N801 - 与 ToolSpec 鸭子兼容
        name = "echo"
        description = "回显输入"
        parameters = {
            "type": "object",
            "properties": {"text": {"type": "string", "description": "原文"}},
            "required": ["text"],
        }

    async def run(self, **kwargs):
        return f"ECHO::{kwargs.get('text', '')}"


def build_loop(llm) -> ReActLoop:
    from harness.guardrails.base import GuardrailPipeline
    from harness.memory.conversation_history import ConversationHistory
    from harness.observability.metrics import MetricsCollector
    from harness.observability.tracer import Tracer

    reg = Registry()
    reg.register_tool(EchoTool())
    return ReActLoop(
        llm=llm, registry=reg, guardrails=GuardrailPipeline(),
        tracer=Tracer(enabled=False), metrics=MetricsCollector(),
        conversation_history=ConversationHistory(), max_iterations=4,
    )


async def collect(loop: ReActLoop, text: str) -> AgentResult:
    result = None
    async for ev in loop.execute_stream(text, session_id="test-fc"):
        if ev["type"] in ("result", "error"):
            result = ev["result"]
    assert result is not None
    return result


def test_registry_openai_tools_schema():
    reg = Registry()
    reg.register_tool(EchoTool())
    tools = reg.get_openai_tools()
    assert len(tools) == 1
    fn = tools[0]
    assert fn["type"] == "function"
    assert fn["function"]["name"] == "echo"
    assert fn["function"]["parameters"]["required"] == ["text"]


@pytest.mark.asyncio
async def test_native_tool_call_executes_and_feeds_result_back():
    """结构化调用直接执行；工具返回回灌下一轮上下文；全程无文本协议痕迹"""
    llm = FakeLLM([
        {"id": "c1", "name": "echo", "arguments": {"text": "你好世界"}},
        "已为您回显内容",
    ])
    loop = build_loop(llm)
    result = await collect(loop, "帮我回显")

    executed = [s for s in result.steps if s.tool_call is not None]
    assert executed and executed[0].tool_call.tool_name == "echo"
    assert executed[0].tool_call.arguments == {"text": "你好世界"}
    assert result.success
    assert llm.calls[0]["tools"], "必须携带 tools 载荷"
    # 工具真实返回回灌进第二次调用的消息里
    assert len(llm.calls) >= 2 and any(
        "ECHO::你好世界" in m.content for m in llm.calls[1]["messages"])
    assert all("ACTION" not in (s.thought or "") for s in result.steps)


@pytest.mark.asyncio
async def test_plain_content_is_final_answer():
    """模型不发起工具调用 → 内容经护栏后作为最终回答"""
    llm = FakeLLM(["你好，有什么可以帮助您的？"])
    loop = build_loop(llm)
    result = await collect(loop, "你好")

    assert result.success
    assert "你好" in result.answer
    assert all(s.tool_call is None for s in result.steps)


@pytest.mark.asyncio
async def test_unsupported_endpoint_raises_clear_error():
    """端点不支持 tools：不再静默降级，向上抛出含特征信息的 LLMError"""
    llm = FakeLLM([LLMError("HTTP 400: tools is not supported")])
    loop = build_loop(llm)
    result = await collect(loop, "触发")

    assert not result.success
    assert "tools" in (result.error or "")


@pytest.mark.asyncio
async def test_bad_native_args_go_through_retry_chain():
    """native 参数缺必填 → ToolError → 失败修正重试 → 第二次结构化修正成功"""
    llm = FakeLLM([
        {"id": "c1", "name": "echo", "arguments": {}},  # 缺必填 text
        {"id": "c2", "name": "echo", "arguments": {"text": "修正后"}},
        "已完成",
    ])
    loop = build_loop(llm)
    result = await collect(loop, "再来一次")

    executed = [s for s in result.steps if s.tool_call is not None]
    assert executed and executed[0].tool_call.tool_name == "echo"
    assert result.success
    assert llm.calls[0]["tools"] and llm.calls[1]["tools"]


@pytest.mark.asyncio
async def test_respond_finalizes_answer():
    """终态工具 respond 承载最终回复；不出现裸文本回答"""
    llm = FakeLLM([
        {"id": "r1", "name": "respond", "arguments": {"content": "这是最终回复内容"}},
    ])
    loop = build_loop(llm)
    result = await collect(loop, "你好")

    assert result.success
    assert result.answer == "这是最终回复内容"
    # 终态 step 以 respond 工具记录
    final_steps = [s for s in result.steps if s.tool_call is not None]
    assert final_steps and final_steps[-1].tool_call.tool_name == "respond"


@pytest.mark.asyncio
async def test_plan_proposes_and_awaits_user():
    """需确认的操作先 plan 提案并向用户提问，不直接执行工具"""
    llm = FakeLLM([
        {"id": "p1", "name": "plan",
         "arguments": {"actions": ["echo 执行某操作"], "message": "请确认是否执行？"}},
    ])
    loop = build_loop(llm)
    result = await collect(loop, "帮我做点事")

    assert result.success
    # 答案即提案提问语，未真正执行 echo
    assert result.answer == "请确认是否执行？"
    assert not any(s.tool_call is not None and s.tool_call.tool_name == "echo"
                   for s in result.steps)
    plan_steps = [s for s in result.steps if s.tool_call is not None]
    assert plan_steps and plan_steps[-1].tool_call.tool_name == "plan"


@pytest.mark.asyncio
async def test_multiple_tool_calls_execute_in_one_round():
    """一轮内多个工具调用顺序执行，结果一并回灌下一轮"""
    llm = FakeLLM([
        [
            {"id": "a", "name": "echo", "arguments": {"text": "第一件"}},
            {"id": "b", "name": "echo", "arguments": {"text": "第二件"}},
        ],
        {"id": "r", "name": "respond", "arguments": {"content": "已全部处理"}},
    ])
    loop = build_loop(llm)
    result = await collect(loop, "都处理一下")

    assert result.success
    executed = [s for s in result.steps if s.tool_call is not None]
    # 两个 echo 在同一轮（round_index 相同）顺序执行，后接 respond 终态
    echo_steps = [s for s in executed if s.tool_call.tool_name == "echo"]
    assert len(echo_steps) == 2
    assert echo_steps[0].round_index == echo_steps[1].round_index
    fed = " ".join(m.content for m in llm.calls[1]["messages"])
    assert "第一件" in fed
    assert "第二件" in fed
