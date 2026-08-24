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

SUBTASK_SYSTEM_PROMPT = """你是智能体助手，请完成以下子任务。
可使用以下工具：{tool_descriptions}

请根据任务需要调用合适的工具。调用格式：

THOUGHT: 分析
ACTION: {{"tool": "工具名", "arguments": {{"key": "value"}}}}

收到工具返回后，继续分析是否需要调用其他工具。
如果已获得足够信息，直接回答结果。"""

# 禁止子任务递归调用的工具名列表
_FORBIDDEN_SUBTOOLS = {"subtask_dispatch"}


class SubTaskDispatchTool(BaseTool):
    """子任务分发：为每个子任务构建隔离的注册中心与记忆，逐个执行并汇总

    执行模型：外层 for 循环每轮 = 一次 LLM 决策；
    - 输出 ACTION → 校验/执行工具 → 结果写入子任务记忆 → 进入下一轮
    - 无 ACTION   → 视为子任务最终结论，结束该子任务
    - 超过迭代上限 → 标记未完成
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

    @staticmethod
    def _parse_tool_call(text: str) -> ToolCall | None:
        match = re.search(r"ACTION:\s*", text)
        if not match:
            return None
        decoder = json.JSONDecoder()
        start = match.end()
        try:
            obj, _ = decoder.raw_decode(text, start)
            return ToolCall(
                tool_name=obj.get("tool", obj.get("name", "")),
                arguments=obj.get("arguments", {}),
            )
        except (json.JSONDecodeError, KeyError) as e:
            logger.warning(
                "子任务解析工具调用失败: %s | text=%s", e, text[match.end():match.end() + 80]
            )
            return None

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

            tool_desc = sub_registry.get_tool_descriptions() or "（无可用工具）"
            system_prompt = SUBTASK_SYSTEM_PROMPT.format(tool_descriptions=tool_desc)

            memory = ShortTermMemory(window_size=10)
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

        for step in range(max_iterations):
            messages = [
                AgentMessage(role=ChatRole.system, content=system_prompt),
                *memory.get_context(),
            ]

            reply = await self._llm.chat_async(messages, temperature=settings.temperature)
            thought = reply.content
            tool_call = self._parse_tool_call(thought)

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
                reply = await self._llm.chat_async(messages, temperature=settings.temperature)
                thought = reply.content
                new_call = self._parse_tool_call(thought)
                if new_call is None:
                    logger.info("子任务 %s 工具失败后 LLM 给出结论: %s", task_id, thought[:80])
                    memory.add(AgentMessage(role=ChatRole.assistant, content=thought))
                    return "", thought
                tool_call = new_call
