from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from collections.abc import AsyncGenerator
from typing import Any

from harness.config import settings
from harness.core.registry import Registry
from harness.domain.exceptions import (
    GuardrailError,
    MaxIterationsExceeded,
    ToolError,
    ToolNotFoundError,
)
from harness.domain.models import (
    AgentMessage,
    AgentResult,
    ChatRole,
    StepRecord,
    ToolCall,
    ToolResult,
)
from harness.guardrails.base import GuardrailPipeline
from harness.llm.base import AbstractLLMClient
from harness.memory.conversation_history import ConversationHistory
from harness.memory.long_term import LongTermMemory
from harness.memory.short_term import ShortTermMemory
from harness.observability.metrics import MetricsCollector
from harness.observability.tracer import Tracer
from harness.tools.base import BaseTool

logger = logging.getLogger("harness.core.loop")

SYSTEM_PROMPT_TEMPLATE = """你是专业的电商智能客服助手，名叫小慧。

## 核心能力
你拥有以下工具可用，根据用户问题自主决定是否需要调用工具。

{tool_descriptions}

## 工作方式
1. 如果用户需要查询多个订单、多个物流信息，或涉及多个步骤的复杂任务，请使用subtask_dispatch工具将任务分解；
2. 如果用户问题需要查询信息或执行操作，请调用合适的工具；
3. 调用工具时，请严格按照以下格式输出：

   THOUGHT: 分析用户需求，说明为什么需要调用这个工具
   ACTION: {{"tool": "工具名", "arguments": {{"key": "value"}}}}

4. 收到工具返回结果后，请用自然语言回复用户；
5. 如果不需要调用工具，直接回答用户问题即可。

## 行为准则
- 仅基于已提供的信息回答，不要编造不存在的信息
- 语气亲切、简洁明了，符合电商客服风格
- 无法处理时引导用户联系人工客服

## 预算约束（重要）
- 用户提到具体金额（如"3999的手机"、"5000元以内的笔记本"）时，**严格视为预算上限**
- **预算在多轮对话中持续生效**：如果用户在之前的对话中提到过预算，后续推荐同一品类商品时仍需遵守该预算，除非用户明确变更或取消预算
- **调用 knowledge_retrieval 工具时，务必把上下文中的预算信息合并到 query 参数里**（如用户之前说 3999 预算，现在问"高性能手机"，应传入 query="高性能手机 3999元以内"），让检索工具做价格过滤
- **禁止推荐价格超过预算的商品**，即使用户预算内有更便宜的选项，也不要主动推荐"略超预算"的商品
- 优先推荐**接近预算上限**的商品（让用户觉得钱花得值），而非远低于预算的廉价款
- 如果预算内有多个选项，按价格从高到低排序展示（最接近预算的排第一）
- 如果预算内完全无匹配商品，明确告知"当前价位暂无匹配商品"，再推荐最接近预算的 2-3 款（必须标注"略超预算"），让用户自己决定是否加钱
- 绝不擅自把"3999的手机"理解成"3999 左右"或"3999-5999 都行"

## 空结果处理（重要）
- 当工具返回"暂无匹配的商品"或空结果时，**不要重复用相似关键词重试**
- 应换一种思路：放宽预算区间（如 3000 元以内无结果时，推荐 3000-3500 元最接近的款），或换品类建议
- 最多重试 1 次，若仍无结果，直接告知用户"当前价位暂无匹配商品，为您推荐最接近的款："并列出 2-3 款相近商品
- 绝不能陷入反复查询同一类信息的死循环
"""


class ReActLoop:
    def __init__(
        self,
        llm: AbstractLLMClient,
        registry: Registry,
        guardrails: GuardrailPipeline,
        tracer: Tracer,
        metrics: MetricsCollector,
        conversation_history: ConversationHistory,
        max_iterations: int = 10,
        long_term_memory: LongTermMemory | None = None,
    ) -> None:
        self.llm = llm
        self.registry = registry
        self.guardrails = guardrails
        self.tracer = tracer
        self.metrics = metrics
        self.conversation_history = conversation_history
        self.max_iterations = max_iterations
        self.long_term_memory = long_term_memory

    async def execute(self, user_input: str, session_id: str | None = None) -> AgentResult:
        """非流式执行：消费 execute_stream 并返回最终结果"""
        result: AgentResult | None = None
        async for event in self.execute_stream(user_input, session_id=session_id):
            if event["type"] == "result":
                result = event["result"]
            elif event["type"] == "error":
                result = event["result"]
        return result  # type: ignore[return-value]

    async def execute_stream(
        self, user_input: str, session_id: str | None = None
    ) -> AsyncGenerator[dict[str, Any], None]:
        """流式执行：在每个 ReAct 步骤发生时即时 yield 事件"""
        start_time = time.perf_counter()
        steps: list[StepRecord] = []
        total_tokens = 0

        sid = session_id or self._generate_id()
        memory = ShortTermMemory(window_size=settings.short_term_window)

        if session_id:
            history = self.conversation_history.load(session_id)
            if history:
                for msg in history:
                    memory.add(msg)
                logger.info("已恢复会话 %s: %d 条历史消息", session_id, len(history))

        try:
            validated_input = self.guardrails.check_input(user_input)

            tool_descriptions = self.registry.get_tool_descriptions()
            system_prompt = SYSTEM_PROMPT_TEMPLATE.format(
                tool_descriptions=tool_descriptions or "（当前没有可用工具）"
            )

            # 长期记忆：检索与当前用户输入相关的历史对话，注入 system prompt
            if self.long_term_memory is not None and self.long_term_memory.enabled:
                hits = await self.long_term_memory.search(validated_input)
                if hits:
                    recall_lines = []
                    for i, h in enumerate(hits, 1):
                        recall_lines.append(f"[{i}] {h['document']}")
                    system_prompt += (
                        "\n\n## 相关历史记忆\n"
                        "以下是与当前问题可能相关的历史对话片段，"
                        "若与本次问题相关可参考，无关则忽略：\n"
                        + "\n\n".join(recall_lines)
                    )
                    logger.info("长期记忆召回 %d 条相关历史", len(hits))

            memory.add(AgentMessage(role=ChatRole.user, content=validated_input))
            self.metrics.reset()

            result: AgentResult | None = None

            for step_index in range(self.max_iterations):
                messages = self._build_messages(system_prompt, memory)

                thought = await self.llm.chat_async(messages, temperature=settings.temperature)
                actual_tokens = self.llm.last_token_usage
                self.metrics.record_llm_call(actual_tokens or len(thought) // 4)
                total_tokens += actual_tokens or len(thought) // 4

                tool_call = self._parse_tool_call(thought)

                if tool_call is None:
                    memory.add(AgentMessage(role=ChatRole.assistant, content=thought))
                    self.tracer.record_step(step_index, thought, None, None)
                    steps.append(StepRecord(step_index=step_index, thought=thought))

                    # 直接回答也推送 step 事件，让前端看到 LLM 的思考过程
                    step_payload: dict[str, Any] = {
                        "type": "step",
                        "step_index": step_index,
                        "thought": thought,
                    }
                    yield step_payload
                    await asyncio.sleep(0.001)

                    duration = (time.perf_counter() - start_time) * 1000
                    self.metrics.record_duration(duration)

                    result = AgentResult(
                        answer=thought,
                        steps=steps,
                        total_duration_ms=round(duration, 2),
                        total_tokens=total_tokens,
                        success=True,
                    )
                    break
                else:
                    self.metrics.record_tool_call(tool_call.tool_name)
                    max_r = settings.tool_max_retries
                    retry_count = 0

                    while True:
                        try:
                            tool = self.registry.get_tool(tool_call.tool_name)
                            self._validate_tool_args(tool, tool_call.arguments)

                            tool_start = time.perf_counter()
                            output = await tool.run(**tool_call.arguments)
                            tool_duration = (time.perf_counter() - tool_start) * 1000

                            tool_result = ToolResult(
                                tool_call_id=tool_call.id,
                                tool_name=tool_call.tool_name,
                                success=True,
                                output=str(output),
                                duration_ms=round(tool_duration, 2),
                            )
                            break
                        except ToolNotFoundError:
                            tool_result = ToolResult(
                                tool_call_id=tool_call.id,
                                tool_name=tool_call.tool_name,
                                success=False,
                                output=f"工具 '{tool_call.tool_name}' 不存在",
                            )
                            break
                        except (ToolError, Exception) as e:
                            retry_count += 1
                            if retry_count > max_r:
                                logger.warning("工具 %s 重试 %d 次后放弃: %s",
                                               tool_call.tool_name, retry_count, e)
                                tool_result = ToolResult(
                                    tool_call_id=tool_call.id,
                                    tool_name=tool_call.tool_name,
                                    success=False,
                                    output=str(e),
                                )
                                break

                            logger.warning("工具 %s 执行失败(第%d次), 交由 LLM 修正: %s",
                                           tool_call.tool_name, retry_count, e)
                            memory.add(AgentMessage(
                                role=ChatRole.tool,
                                content=f"[工具 {tool_call.tool_name}] 执行失败(重试{retry_count}/{max_r}): {e}",
                                tool_name=tool_call.tool_name,
                                tool_call_id=tool_call.id,
                            ))

                            messages = self._build_messages(system_prompt, memory)
                            thought = await self.llm.chat_async(messages, temperature=settings.temperature)
                            actual_tokens = self.llm.last_token_usage
                            self.metrics.record_llm_call(actual_tokens or len(thought) // 4)
                            total_tokens += actual_tokens or len(thought) // 4

                            new_call = self._parse_tool_call(thought)
                            if new_call is None:
                                tool_result = ToolResult(
                                    tool_call_id=tool_call.id,
                                    tool_name=tool_call.tool_name,
                                    success=False,
                                    output="工具执行失败后 LLM 放弃重试",
                                )
                                break
                            tool_call = new_call
                            self.metrics.record_tool_call(tool_call.tool_name)
                            continue

                    tool_result.output = self.guardrails.check_tool_output(tool_result.output)

                    memory.add(AgentMessage(role=ChatRole.assistant, content=thought))
                    memory.add(AgentMessage(
                        role=ChatRole.tool,
                        content=f"[工具 {tool_call.tool_name}] 返回: {tool_result.output}",
                        tool_name=tool_call.tool_name,
                        tool_call_id=tool_call.id,
                    ))

                    self.tracer.record_step(step_index, thought, tool_call, tool_result)
                    steps.append(StepRecord(
                        step_index=step_index,
                        thought=thought,
                        tool_call=tool_call,
                        tool_result=tool_result,
                    ))

                    # 即时推送本步骤
                    step_payload: dict[str, Any] = {"type": "step", "step_index": step_index}
                    if thought:
                        step_payload["thought"] = thought
                    if tool_call:
                        step_payload["tool_call"] = tool_call.model_dump()
                    if tool_result:
                        step_payload["tool_result"] = tool_result.model_dump()
                    yield step_payload
                    await asyncio.sleep(0.001)
            else:
                raise MaxIterationsExceeded(f"超过最大迭代次数 ({self.max_iterations})")

            assert result is not None
            result.answer = self.guardrails.check_output(result.answer)
            self.conversation_history.save(sid, memory.get_context())

            # 长期记忆：将本轮完整对话（用户输入 + 最终回答）异步写入向量库
            # 用 create_task 后台执行，不阻塞响应流
            if self.long_term_memory is not None and self.long_term_memory.enabled:
                asyncio.create_task(
                    self.long_term_memory.add(
                        user_input=validated_input,
                        assistant_answer=result.answer,
                        session_id=sid,
                    )
                )

            yield {
                "type": "result",
                "result": result,
                "answer": result.answer,
                "total_duration_ms": result.total_duration_ms,
                "total_steps": len(result.steps),
                "success": result.success,
            }

        except GuardrailError as e:
            logger.warning("Guardrail 拦截: %s", e)
            err = AgentResult(answer=str(e), steps=steps, success=False, error=str(e))
            yield {"type": "error", "result": err, "message": str(e)}
        except MaxIterationsExceeded as e:
            logger.warning("Agent 循环终止: %s", e)
            err = AgentResult(
                answer="抱歉，您的请求需要多次查询，请简化问题后重试或联系人工客服。",
                steps=steps, success=False, error=str(e),
            )
            yield {"type": "error", "result": err, "message": str(err.answer)}
        except Exception as e:
            logger.exception("Agent 执行异常")
            err = AgentResult(answer=f"系统异常: {e}", steps=steps, success=False, error=str(e))
            yield {"type": "error", "result": err, "message": str(e)}

    @staticmethod
    def _build_messages(system_prompt: str, memory: ShortTermMemory) -> list[AgentMessage]:
        return [
            AgentMessage(role=ChatRole.system, content=system_prompt),
            *memory.get_context(),
        ]

    @staticmethod
    def _generate_id() -> str:
        import hashlib
        from datetime import datetime
        raw = f"{datetime.now().isoformat()}"
        return hashlib.md5(raw.encode()).hexdigest()[:12]

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
            logger.warning("解析工具调用失败: %s | text=%s", e, text[match.end() : match.end() + 80])
            return None

    @staticmethod
    def _validate_tool_args(tool: BaseTool, args: dict[str, Any]) -> None:
        required = tool.spec.parameters.get("required", [])
        for key in required:
            if key not in args or (isinstance(args[key], str) and not args[key].strip()):
                raise ToolError(f"工具 '{tool.spec.name}' 缺少必填参数: {key}")
