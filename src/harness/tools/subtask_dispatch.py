from __future__ import annotations

import json
import logging
import re
import time
from typing import TYPE_CHECKING, Any

from harness.domain.models import AgentMessage, ChatRole, ToolCall
from harness.llm.base import AbstractLLMClient
from harness.memory.short_term import ShortTermMemory
from harness.tools.base import BaseTool, ToolSpec

if TYPE_CHECKING:
    from harness.core.registry import Registry
    from harness.guardrails.base import GuardrailPipeline

logger = logging.getLogger("harness.tools.subtask_dispatch")

SUBTASK_SYSTEM_PROMPT = """你是子任务执行器，只负责完成下述这一个子任务。
可用工具以本次请求携带的 tools 定义为准。

执行规则：
1. 每次调用前用一句话说明要做什么；收到结果后判断是否已足够完成任务；
2. 信息足够时立即输出**结论**：直接给答案与关键数据（单号/状态/金额），
   不要寒暄、不要复述任务、不要说"我将继续"；
3. 工具连续 2 次失败或缺少必要条件：如实输出「无法完成 + 原因 + 已尝试什么」，
   **绝不编造数据充数**；
4. 只使用分配给你的工具，不要假设存在其他能力。

【子任务】{description}"""

# 禁止子任务递归调用的工具名列表
_FORBIDDEN_SUBTOOLS = {"subtask_dispatch"}


class SubTaskDispatchTool(BaseTool):
    """子任务分发：为每个子任务构建隔离的注册中心与记忆，逐个执行并汇总

    执行模型：外层 for 循环每轮 = 一次 LLM 决策（原生 function calling）；
    - 返回结构化 tool_calls → 校验/执行工具 → 结果写入子任务记忆 → 下一轮
    - 无工具调用       → 视为子任务最终结论，结束该子任务
    - 超过迭代上限     → 标记未完成
    """

    spec = ToolSpec(
        name="subtask_dispatch",
        description="将复杂任务拆分为多个子任务，逐一分发给子智能体执行并汇总结果",
        parameters={
            "type": "object",
            "properties": {
                "tasks": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "id": {"type": "string", "description": "子任务唯一标识"},
                            "description": {"type": "string", "description": "子任务描述"},
                            "tools": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": "子任务可用的工具名列表",
                            },
                        },
                        "required": ["id", "description", "tools"],
                    },
                    "description": "子任务列表",
                },
            },
            "required": ["tasks"],
        },
    )

    def __init__(
        self,
        llm: AbstractLLMClient | None = None,
        registry: Registry | None = None,
        guardrails: GuardrailPipeline | None = None,
    ) -> None:
        self._llm = llm
        self._parent_registry = registry
        self._guardrails = guardrails

    def _mask(self, text: str) -> str:
        if self._guardrails is not None:
            try:
                return self._guardrails.check_tool_output(text)
            except Exception:
                return text
        return text

    async def run(self, **kwargs: Any) -> str:
        tasks: list[dict[str, Any]] = kwargs.get("tasks") or []
        if not tasks:
            return "未提供子任务"
        if self._llm is None:
            return "子任务调度器未配置 LLM，无法执行"
        if self._parent_registry is None:
            return "子任务调度器未关联主注册中心，无法执行"

        from harness.config import settings

        results: dict[str, str] = {}

        for task in tasks[:10]:  # 上限保护：防止一次分发过多子任务
            task_id = str(task.get("id", "unknown"))
            description = str(task.get("description", ""))
            tool_names: list[str] = list(task.get("tools") or [])

            # 过滤禁止递归调用的工具
            safe_tool_names = [n for n in tool_names if n not in _FORBIDDEN_SUBTOOLS]
            filtered = set(tool_names) - set(safe_tool_names)
            if filtered:
                logger.warning("子任务 %s 过滤了禁止的工具: %s", task_id, filtered)

            sub_registry = type(self._parent_registry)()
            for name in safe_tool_names:
                try:
                    sub_registry.register_tool(self._parent_registry.get_tool(name))
                except Exception:
                    logger.warning("子任务 %s 缺少工具 %s", task_id, name)

            # 工具清单由 tools 载荷原生携带；子任务描述注入提示词
            system_prompt = SUBTASK_SYSTEM_PROMPT.format(description=description)

            memory = ShortTermMemory()
            memory.add(AgentMessage(role=ChatRole.user, content=description))

            # 可用工具数 * 2 + 2：支持同一工具多次调用；无工具时只给一轮作答机会
            max_iterations = max(len(safe_tool_names) * 2 + 2, 4) if safe_tool_names else 3

            results[task_id] = await self._execute_single_task(
                task_id=task_id,
                description=description,
                system_prompt=system_prompt,
                registry=sub_registry,
                memory=memory,
                max_iterations=max_iterations,
                max_retries=settings.tool_max_retries,
            )

        return json.dumps(results, ensure_ascii=False)

    async def _execute_single_task(
        self,
        task_id: str,
        description: str,
        system_prompt: str,
        registry: Any,
        memory: ShortTermMemory,
        max_iterations: int,
        max_retries: int,
    ) -> str:
        from harness.config import settings

        # 原生 function calling：子注册中心的工具转 OpenAI schema
        tools = registry.get_openai_tools()

        for step in range(max_iterations):
            messages = [
                AgentMessage(role=ChatRole.system, content=system_prompt),
                *memory.get_context(),
            ]
            tc_sink: dict = {}
            reply = await self._llm.chat_async(
                messages, temperature=settings.temperature,
                tools=tools or None, tool_call_sink=tc_sink if tools else None,
            )
            thought = reply.content

            native_calls = (tc_sink.get("tool_calls") if tools else None) \
                or reply.tool_calls
            tool_call: ToolCall | None = None
            if native_calls:
                nc = native_calls[0]
                thought = thought or f"调用工具 {nc['name']}"
                tool_call = ToolCall(tool_name=nc["name"], arguments=nc["arguments"])

            # 无工具调用 → 子任务完成，返回最终结论
            if tool_call is None:
                memory.add(AgentMessage(role=ChatRole.assistant, content=thought))
                logger.info("子任务 %s 完成 (step %d): %s", task_id, step, thought[:80])
                return thought

            # 运行期兜底：即使注册时过滤过，也拦截递归调用
            if tool_call.tool_name in _FORBIDDEN_SUBTOOLS:
                memory.add(AgentMessage(role=ChatRole.assistant, content=thought))
                memory.add(AgentMessage(
                    role=ChatRole.tool,
                    content=f"[系统] 不允许在子任务中调用 '{tool_call.tool_name}' 工具",
                ))
                continue

            output, failed_msg = await self._run_tool_with_retry(
                task_id, step, system_prompt, registry, memory, thought, tool_call, max_retries
            )

            if failed_msg is not None:
                # LLM 放弃修正：把它的思考作为部分结果返回
                return failed_msg

            memory.add(AgentMessage(role=ChatRole.assistant, content=thought))
            memory.add(AgentMessage(
                role=ChatRole.tool,
                content=f"[工具 {tool_call.tool_name}] 返回: {output}",
                tool_name=tool_call.tool_name,
                tool_call_id=tool_call.id,
            ))
            # 继续下一轮：LLM 将看到工具结果并决定继续调用或给出结论
            logger.info("子任务 %s step %d: 调用 %s 成功", task_id, step, tool_call.tool_name)

        logger.warning("子任务 %s 超过最大迭代次数 %d", task_id, max_iterations)
        return "子任务未能在有限步骤内完成"

    async def _run_tool_with_retry(
        self,
        task_id: str,
        step: int,
        system_prompt: str,
        registry: Any,
        memory: ShortTermMemory,
        thought: str,
        initial_call: ToolCall,
        max_retries: int,
    ) -> tuple[str | None, str | None]:
        """执行工具，失败时让 LLM 修正参数重试。

        返回 (output, failed_msg)：
        - 成功 → (输出文本, None)，并把 assistant/tool 消息写入 memory 由调用方处理
        - 彻底失败 → (None, 失败说明或 LLM 的部分结论)
        """
        from harness.config import settings

        # 修正重试路径同样携带原生 tools 载荷（与首轮调用保持一致）
        tools = registry.get_openai_tools()

        tool_call = initial_call
        retry_count = 0

        while True:
            start = time.perf_counter()
            try:
                tool = registry.get_tool(tool_call.tool_name)
                required = tool.spec.parameters.get("required", [])
                for key in required:
                    value = tool_call.arguments.get(key)
                    if key not in tool_call.arguments or (
                        isinstance(value, str) and not value.strip()
                    ):
                        raise ValueError(f"缺少必填参数: {key}")

                output = await tool.run(**tool_call.arguments)
                duration = (time.perf_counter() - start) * 1000
                masked = self._mask(str(output))
                logger.info(
                    "子任务 %s step %d: 调用 %s (%.0fms)",
                    task_id, step, tool_call.tool_name, duration,
                )
                return masked, None

            except Exception as e:
                retry_count += 1
                if retry_count > max_retries:
                    logger.warning(
                        "子任务 %s 工具 %s 重试 %d 次后放弃: %s",
                        task_id, tool_call.tool_name, retry_count - 1, e,
                    )
                    return "", f"[工具 {tool_call.tool_name}] 执行失败: {e}"

                logger.warning(
                    "子任务 %s 工具 %s 失败(第%d次), LLM 修正重试: %s",
                    task_id, tool_call.tool_name, retry_count, e,
                )
                # 把失败的 thought 也写进记忆，LLM 能看到自己上一步想干什么
                memory.add(AgentMessage(role=ChatRole.assistant, content=thought))
                memory.add(AgentMessage(
                    role=ChatRole.tool,
                    content=f"[工具 {tool_call.tool_name}] 执行失败(重试{retry_count}/{max_retries}): {e}",
                    tool_name=tool_call.tool_name,
                    tool_call_id=tool_call.id,
                ))

                messages = [
                    AgentMessage(role=ChatRole.system, content=system_prompt),
                    *memory.get_context(),
                ]
                tc_sink2: dict = {}
                reply = await self._llm.chat_async(
                    messages, temperature=settings.temperature,
                    tools=tools or None, tool_call_sink=tc_sink2 if tools else None,
                )
                thought = reply.content
                native_calls = (tc_sink2.get("tool_calls") if tools else None) \
                    or reply.tool_calls
                if native_calls:
                    nc = native_calls[0]
                    thought = thought or f"调用工具 {nc['name']}"
                    new_call = ToolCall(tool_name=nc["name"], arguments=nc["arguments"])
                else:
                    new_call = None
                if new_call is None:
                    logger.info("子任务 %s 工具失败后 LLM 给出结论: %s", task_id, thought[:80])
                    memory.add(AgentMessage(role=ChatRole.assistant, content=thought))
                    return "", thought
                tool_call = new_call
