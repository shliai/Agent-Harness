from __future__ import annotations

import json
import logging
import re
import time
from typing import Any

from harness.domain.models import AgentMessage, ChatRole, ToolCall
from harness.llm.base import AbstractLLMClient
from harness.memory.short_term import ShortTermMemory
from harness.tools.base import BaseTool, ToolSpec

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
    ) -> None:
        self._llm = llm
        self._parent_registry = registry

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
            logger.warning("子任务解析工具调用失败: %s | text=%s", e, text[match.end():match.end() + 80])
            return None

    async def run(self, **kwargs: Any) -> str:
        tasks: list[dict[str, Any]] = kwargs.get("tasks", [])
        if not tasks:
            return "未提供子任务"

        from harness.config import settings
        from harness.core.registry import Registry

        if self._parent_registry is None:
            return "子任务调度器未关联主注册中心，无法执行"

        results: dict[str, str] = {}
        for task in tasks:
            task_id = task.get("id", "unknown")
            description = task.get("description", "")
            tool_names: list[str] = task.get("tools", [])

            # 过滤掉禁止递归调用的工具
            safe_tool_names = [n for n in tool_names if n not in _FORBIDDEN_SUBTOOLS]
            filtered_count = len(tool_names) - len(safe_tool_names)
            if filtered_count:
                logger.warning("子任务 %s 过滤了 %d 个禁止的工具: %s",
                               task_id, filtered_count, _FORBIDDEN_SUBTOOLS & set(tool_names))

            sub_registry = Registry()
            for name in safe_tool_names:
                try:
                    tool = self._parent_registry.get_tool(name)
                    sub_registry.register_tool(tool)
                except Exception:
                    logger.warning("子任务 %s 缺少工具 %s", task_id, name)

            tool_desc = sub_registry.get_tool_descriptions() or "（无可用工具）"
            system_prompt = SUBTASK_SYSTEM_PROMPT.format(tool_descriptions=tool_desc)

            memory = ShortTermMemory(window_size=10)
            memory.add(AgentMessage(role=ChatRole.user, content=description))

            # 最大迭代次数：可用工具数 * 2 + 2，支持同一工具多次调用
            max_iterations = max(len(safe_tool_names) * 2 + 2, 4) if safe_tool_names else 3

            for step in range(max_iterations):
                messages = [
                    AgentMessage(role=ChatRole.system, content=system_prompt),
                    *memory.get_context(),
                ]

                thought = await self._llm.chat_async(messages, temperature=settings.temperature)
                tool_call = self._parse_tool_call(thought)

                if tool_call is None:
                    # 子任务完成，无工具调用
                    memory.add(AgentMessage(role=ChatRole.assistant, content=thought))
                    results[task_id] = thought
                    logger.info("子任务 %s 完成: %s", task_id, thought[:80])
                    break

                # 子任务中不能递归调用 subtask_dispatch
                if tool_call.tool_name in _FORBIDDEN_SUBTOOLS:
                    memory.add(AgentMessage(role=ChatRole.assistant, content=thought))
                    memory.add(AgentMessage(
                        role=ChatRole.tool,
                        content=f"[系统] 不允许在子任务中调用 '{tool_call.tool_name}' 工具",
                    ))
                    continue

                # 执行工具（含失败重试）
                retry_count = 0
                max_retries = settings.tool_max_retries

                while True:
                    try:
                        tool = sub_registry.get_tool(tool_call.tool_name)
                        required = tool.spec.parameters.get("required", [])
                        for key in required:
                            if key not in tool_call.arguments or (
                                isinstance(tool_call.arguments[key], str)
                                and not tool_call.arguments[key].strip()
                            ):
                                raise ValueError(f"缺少必填参数: {key}")

                        start = time.perf_counter()
                        output = await tool.run(**tool_call.arguments)
                        duration = (time.perf_counter() - start) * 1000

                        memory.add(AgentMessage(role=ChatRole.assistant, content=thought))
                        memory.add(AgentMessage(
                            role=ChatRole.tool,
                            content=f"[工具 {tool_call.tool_name}] 返回: {output}",
                            tool_name=tool_call.tool_name,
                            tool_call_id=tool_call.id,
                        ))

                        logger.info("子任务 %s step %d: 调用 %s (%.0fms)",
                                    task_id, step, tool_call.tool_name, duration)
                        break
                    except Exception as e:
                        retry_count += 1
                        if retry_count > max_retries:
                            logger.warning("子任务 %s 工具 %s 重试 %d 次后放弃: %s",
                                           task_id, tool_call.tool_name, retry_count, e)
                            memory.add(AgentMessage(role=ChatRole.assistant, content=thought))
                            memory.add(AgentMessage(
                                role=ChatRole.tool,
                                content=f"[工具 {tool_call.tool_name}] 执行失败(已重试{retry_count-1}次): {e}",
                                tool_name=tool_call.tool_name,
                                tool_call_id=tool_call.id,
                            ))
                            break

                        logger.warning("子任务 %s 工具 %s 失败(第%d次), LLM 修正重试: %s",
                                       task_id, tool_call.tool_name, retry_count, e)
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
                        thought = await self._llm.chat_async(messages, temperature=settings.temperature)
                        new_call = self._parse_tool_call(thought)
                        if new_call is None:
                            memory.add(AgentMessage(role=ChatRole.assistant, content=thought))
                            results[task_id] = thought
                            logger.info("子任务 %s 工具失败后 LLM 放弃重试: %s", task_id, thought[:80])
                            break
                        tool_call = new_call
                        continue
                else:
                    # while 正常结束（没有 break），继续外层 for 循环
                    continue
                # while 被 break，跳出外层 for 循环
                break
            else:
                # 超过最大迭代次数
                results[task_id] = "子任务未能在有限步骤内完成"
                logger.warning("子任务 %s 超过最大迭代次数 %d", task_id, max_iterations)

        return json.dumps(results, ensure_ascii=False)