"""D6 人机协同（HITL）评测层：验证任务列表式 ReAct 的「提案 → 执行」闭环。

核心断言：
1. 需要确认 / 缺参数 / 有副作用的操作，模型先调用 plan 提案并向用户提问（不直接执行）；
2. 用户回复后，模型输出带正确参数的最终执行工具列表并执行；
3. 终态用 respond 承载，不出现裸文本回答。

本层用「脚本化 LLM」确定性驱动两轮交互，不消耗真实 LLM tokens，作为该机制的回归护栏。
"""
from __future__ import annotations

from typing import Any

from harness.core.loop import FINALIZE_TOOL, PLAN_TOOL, ReActLoop
from harness.core.registry import Registry
from harness.guardrails.base import GuardrailPipeline
from harness.memory.conversation_history import ConversationHistory
from harness.observability.metrics import MetricsCollector
from harness.observability.tracer import Tracer
from harness.tools.base import BaseTool, ToolSpec


class _HITLTool(BaseTool):
    spec = ToolSpec(
        name="mock_tool",
        description="测试用工具",
        parameters={
            "type": "object",
            "properties": {"input": {"type": "string"}},
            "required": ["input"],
        },
    )

    async def run(self, **kwargs: str) -> str:
        return f"DONE::{kwargs.get('input', '')}"


class _ScriptedHITLLLM:
    """按轮次脚本返回工具调用列表；不读上下文，纯确定性。"""

    def __init__(self, turns: list[list[dict]]) -> None:
        self._turns = turns
        self._i = 0

    async def chat_async(self, *args, **kwargs):
        from harness.llm.base import LLMReply

        return LLMReply(content="", total_tokens=5, tool_calls=self._next())

    async def stream_chat_async(self, *args, **kwargs):
        calls = self._next()
        sink = kwargs.get("tool_call_sink")
        if sink is not None:
            sink["tool_calls"] = calls
        yield ""

    def _next(self) -> list[dict]:
        if self._i >= len(self._turns):
            return []
        item = self._turns[self._i]
        self._i += 1
        return item


def _build_agent(llm: _ScriptedHITLLLM) -> ReActLoop:
    reg = Registry()
    reg.register_tool(_HITLTool())
    return ReActLoop(
        llm=llm, registry=reg, guardrails=GuardrailPipeline(),
        tracer=Tracer(enabled=False), metrics=MetricsCollector(),
        conversation_history=ConversationHistory(), max_iterations=6,
    )


async def _run_turn(loop: ReActLoop, text: str, sid: str):
    result = None
    async for ev in loop.execute_stream(text, session_id=sid):
        if ev["type"] in ("result", "error"):
            result = ev["result"]
    return result


async def eval_hitl(cases: list[dict] | None = None) -> dict:
    results: list[dict] = []

    # 用例：写操作前先 plan，用户确认后才执行
    llm = _ScriptedHITLLLM([
        [{"id": "p", "name": PLAN_TOOL,
          "arguments": {"actions": ["mock_tool 执行写入"], "message": "确认要执行写入吗？"}}],
        [{"id": "m", "name": "mock_tool", "arguments": {"input": "confirmed"}},
         {"id": "r", "name": FINALIZE_TOOL, "arguments": {"content": "已为你执行完成"}}],
    ])
    loop = _build_agent(llm)
    r1 = await _run_turn(loop, "帮我写入数据", "eval-hitl-1")
    r2 = await _run_turn(loop, "确认，执行吧", "eval-hitl-1")

    plan_step = [s for s in r1.steps if s.tool_call and s.tool_call.tool_name == PLAN_TOOL]
    exec_step = [s for s in r2.steps if s.tool_call and s.tool_call.tool_name == "mock_tool"]
    final_step = [s for s in r2.steps if s.tool_call and s.tool_call.tool_name == FINALIZE_TOOL]

    r1_ok = (
        r1.success
        and r1.answer == "确认要执行写入吗？"
        and plan_step
        and not any(s.tool_call and s.tool_call.tool_name == "mock_tool" for s in r1.steps)
    )
    r2_ok = (
        r2.success
        and exec_step
        and final_step
        and r2.answer == "已为你执行完成"
    )
    results.append({
        "id": "hitl_plan_before_write",
        "r1_answer": r1.answer,
        "r2_answer": r2.answer,
        "proposed_before_exec": bool(plan_step),
        "executed_after_confirm": bool(exec_step),
        "pass": bool(r1_ok and r2_ok),
    })

    return _summarize("hitl(人机协同)", results)


def _summarize(layer: str, results: list[dict]) -> dict:
    counted = [r for r in results if not r.get("skip")]
    passed = sum(1 for r in counted if r["pass"])
    return {
        "layer": layer,
        "total": len(counted),
        "skipped": len(results) - len(counted),
        "passed": passed,
        "pass_rate": round(passed / len(counted), 3) if counted else 1.0,
        "cases": results,
    }
