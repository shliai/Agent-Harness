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
from harness.memory.working_memory import WorkingMemory
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

## 推荐引用规范（重要）
- 基于 knowledge_retrieval 结果推荐商品时，**必须在每个商品后附上方括号内的商品编号**，如「小米17 Pro [product_000]」——这是用户核验与售后追溯的依据
- 引用政策条款时同样附上政策编号，如「[POL-REFUND-01]」

## 政策与合规（重要）
- 涉及退换货、保修、价保、发票、配送时效等政策条款的问题，**必须先调用 policy_query 查询官方政策，再严格依据返回条款回答**；禁止凭记忆编造或扩展政策
- 订单与物流查询仅限本人数据，系统会自动校验归属；遇到"不属于当前账户"的提示时如实转达即可

## 售后流程（after_sale_apply / after_sale_query 工具）
- 用户想退货/换货：先确认订单归属与状态（可先用 order_list 帮用户找单），再调用 after_sale_apply 提交，并告知售后单号
- 查询售后进度用 after_sale_query；退款到账时效等条款以 policy_query 返回为准
- 待发货订单的变更诉求（改地址/取消）不要提交售后，直接转人工

## 澄清式追问协议（重要）
- 用户请求缺少必要信息时（如查物流没给单号、报售后没说订单），优先用工具补全（order_list 列出订单让用户选）
- 确实无法从任何工具获得时，**一次性提出一个明确的澄清问题**，等用户回复后再行动；禁止连环追问超过 1 次
- 若「任务状态」中标注了等待补充的信息且用户本轮已给出，直接使用，不要重复询问

## 转人工（transfer_human 工具）
出现以下任一情况应调用 transfer_human：
- 用户明确要求人工客服
- 同一问题尝试 2 次仍无法解决
- 涉及投诉、赔偿、超期纠纷等你无权处理的事项
调用后告知用户工单号，不要代替人工做任何承诺。

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

# ACTION 关键字出现但 JSON 解析失败时的纠正提示
_CLARIFY_RE = re.compile(
    r"(?:请|麻烦|需要您)?(?:提供|告知|告诉我)[^，。？！]{0,12}?(订单号|物流单号|快递单号|订单编号|型号|问题)?"
)

_MALFORMED_FALLBACK_ANSWER = (
    "抱歉，处理您的请求时遇到了内部格式问题。"
    "请换个说法再试一次，或输入「转人工」由人工客服为您处理。"
)

_MALFORMED_ACTION_HINT = (
    "[系统] 上一步输出包含 ACTION，但 JSON 格式无法解析。"
    "请重新输出，严格遵循格式：ACTION: {{\"tool\": \"工具名\", \"arguments\": {{...}}}}"
)


def estimate_tokens(text: str) -> int:
    """粗略估算：CJK ≈0.7 token/字，ASCII ≈4 chars/token（对齐主流中英混合分词器）"""
    if not text:
        return 0
    cjk = sum(1 for ch in text if ord(ch) > 0x2E7F)
    ascii_len = len(text) - cjk
    est = int(cjk * 0.7) + (ascii_len + 3) // 4
    return max(est, 1)


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
        self.metrics = metrics  # 进程级聚合指标（只累加，不 reset）
        self.conversation_history = conversation_history
        self.max_iterations = max_iterations
        self.long_term_memory = long_term_memory
        self._bg_tasks: set[asyncio.Task] = set()  # 后台任务持引用，防 GC

    async def execute(
        self, user_input: str, session_id: str | None = None, user_id: str | None = None
    ) -> AgentResult:
        """非流式执行：消费 execute_stream 并返回最终结果"""
        result: AgentResult | None = None
        async for event in self.execute_stream(user_input, session_id=session_id, user_id=user_id):
            if event["type"] == "result":
                result = event["result"]
            elif event["type"] == "error":
                result = event["result"]
        if result is None:
            raise MaxIterationsExceeded("Agent 未产生任何结果")
        return result

    async def execute_stream(
        self,
        user_input: str,
        session_id: str | None = None,
        user_id: str | None = None,
    ) -> AsyncGenerator[dict[str, Any], None]:
        """流式执行：在每个 ReAct 步骤发生时即时 yield 事件"""
        from harness.tools.context import DEFAULT_USER, current_session_id, current_user_id

        start_time = time.perf_counter()
        steps: list[StepRecord] = []
        req_metrics = MetricsCollector()  # 请求级指标：并发请求互不污染
        total_tokens = 0

        sid = session_id or self._generate_id()
        uid = user_id or DEFAULT_USER
        self._owner_uid = uid  # 本次会话归属（落盘与越权校验依据）
        # 请求级身份上下文：订单归属校验 / 我的订单 / 工单归属 都从这里取
        current_user_id.set(uid)
        current_session_id.set(sid)
        memory = ShortTermMemory(window_size=settings.short_term_window, track_full=True)

        # 恢复会话完整状态：消息全量 + 滚动摘要 + 工作记忆槽位 + 历史推理轨迹
        prior_summary: str | None = None
        prev_traces: list[dict] = []
        wm = WorkingMemory()
        if session_id:
            state = await self.conversation_history.aload_state(session_id)
            if state:
                for raw_msg in state.get("messages", []):
                    try:
                        memory.add(AgentMessage(**raw_msg))
                    except Exception:
                        logger.warning("跳过无法解析的历史消息: %s", str(raw_msg)[:80])
                prior_summary = (state.get("summary") or "").strip() or None
                prev_traces = list(state.get("traces") or [])[-8:]
                wm = WorkingMemory.from_dict(state.get("working_memory"))
                logger.info("已恢复会话 %s: %d 条历史消息", session_id, len(memory.all_messages()))

        # 预算槽位供检索工具确定性注入价格条件；用量槽供流式回写真实 usage
        from harness.tools.context import current_budget, llm_usage_sink

        current_budget.set(wm.budget_amount)
        _llm_usage_sink_var = llm_usage_sink

        try:
            validated_input = self.guardrails.check_input(user_input, session_id=sid)

            tool_descriptions = self.registry.get_tool_descriptions()
            system_prompt = SYSTEM_PROMPT_TEMPLATE.format(
                tool_descriptions=tool_descriptions or "（当前没有可用工具）"
            )

            # 上下文工程：滚动摘要（更早的、已压缩出窗口的对话）
            if prior_summary:
                system_prompt += (
                    "\n\n## 更早对话的摘要\n"
                    "以下是对较早对话的压缩摘要，其中的事实（预算、单号等）仍然有效：\n"
                    + prior_summary
                )

            # 上下文工程：跨轮工作记忆（预算/订单号/物流号，确定性规则维护）
            turn_no = len(memory.all_messages()) + 1
            wm.update_from_input(validated_input, turn=turn_no)
            wm_block = wm.prompt_block()
            if wm_block:
                system_prompt += "\n\n" + wm_block

            # 长期记忆：检索与当前输入相关的历史对话，注入 system prompt
            if self.long_term_memory is not None and self.long_term_memory.enabled:
                hits = await self.long_term_memory.search(validated_input, user_id=uid)
                if hits:
                    recall_lines = [f"[{i}] {h['document']}" for i, h in enumerate(hits, 1)]
                    system_prompt += (
                        "\n\n## 相关历史记忆\n"
                        "以下是与当前问题可能相关的历史对话片段，"
                        "若与本次问题相关可参考，无关则忽略：\n"
                        + "\n\n".join(recall_lines)
                    )
                    logger.info("长期记忆召回 %d 条相关历史", len(hits))

            memory.add(AgentMessage(role=ChatRole.user, content=validated_input))

            result: AgentResult | None = None
            malformed_retries = 0

            for step_index in range(self.max_iterations):
                messages = self._build_messages(system_prompt, memory)

                # token 级流式：增量即时推送前端；若本轮实际是工具规划，
                # 由 delta_reset 事件通知前端回滚临时文本
                buf: list[str] = []
                usage_sink: dict = {}
                _llm_usage_sink_var.set(usage_sink)
                async for delta in self.llm.stream_chat_async(
                    messages, temperature=settings.temperature
                ):
                    buf.append(delta)
                    yield {"type": "delta", "content": delta}
                thought = "".join(buf).strip()

                # 统计口径：优先供应商真实 usage（含 prompt 侧）；
                # 流式未返回时按 prompt + completion 双侧估算
                real = usage_sink.get("total") or 0
                if real:
                    tokens = real
                else:
                    prompt_est = estimate_tokens("".join(m.content for m in messages))
                    tokens = prompt_est + estimate_tokens(thought)
                self._record_llm(req_metrics, tokens)
                total_tokens += tokens

                self._account_budget(sid, wm, tokens)
                tool_call = self._parse_tool_call(thought)

                if tool_call is None and self._looks_like_action(thought):
                    # ACTION 存在但 JSON 解析失败 → 让 LLM 重试而不是把原文当答案
                    malformed_retries += 1
                    if malformed_retries <= 2:
                        logger.warning("ACTION JSON 解析失败(第%d次)，要求重试", malformed_retries)
                        yield {"type": "delta_reset", "reason": "retry"}
                        memory.add(AgentMessage(role=ChatRole.assistant, content=thought))
                        memory.add(AgentMessage(role=ChatRole.tool, content=_MALFORMED_ACTION_HINT))
                        continue
                    # 连续失败：回滚流式文本并降级为友好提示，绝不输出原始残片
                    logger.warning("ACTION 解析连续失败，降级为友好提示")
                    yield {"type": "delta_reset", "reason": "retry"}
                    thought = _MALFORMED_FALLBACK_ANSWER

                if tool_call is None:
                    filtered = self.guardrails.check_output(thought, session_id=sid)
                    if not filtered.strip():
                        filtered = "（本次未产生有效回复，请换个问法或稍后再试。）"
                    if filtered != thought:
                        yield {"type": "answer_replace", "content": filtered}
                    # 先脱敏再入记忆/历史，防止敏感信息经上下文回流
                    memory.add(AgentMessage(role=ChatRole.assistant, content=filtered))
                    self.tracer.record_step(step_index, thought, None, None, session_id=sid)
                    steps.append(StepRecord(step_index=step_index, thought=thought))

                    yield {
                        "type": "step",
                        "step_index": step_index,
                        "thought": thought,
                        "final": True,
                    }
                    await asyncio.sleep(0.001)

                    duration = (time.perf_counter() - start_time) * 1000
                    self._record_duration(req_metrics, duration)

                    # 澄清式反问检测：记录等待项，下一轮注入任务状态防止重复追问
                    clarify = _CLARIFY_RE.search(filtered)
                    if clarify:
                        wm.set_awaiting(clarify.group(1) or "相关信息")

                    result = AgentResult(
                        answer=filtered,
                        steps=steps,
                        total_duration_ms=round(duration, 2),
                        total_tokens=total_tokens,
                        success=True,
                    )
                    break

                # ── 工具调用路径 ──
                step_result = await self._execute_tool_step(
                    sid=sid,
                    step_index=step_index,
                    system_prompt=system_prompt,
                    memory=memory,
                    initial_thought=thought,
                    initial_call=tool_call,
                    req_metrics=req_metrics,
                )
                total_tokens += step_result["extra_tokens"]
                self._account_budget(sid, wm, step_result["extra_tokens"])
                steps.append(step_result["record"])
                self.tracer.record_step(
                    step_index, step_result["final_thought"],
                    step_result["tool_call"], step_result["tool_result"], session_id=sid,
                )
                payload: dict[str, Any] = {
                    "type": "step",
                    "step_index": step_index,
                    "thought": step_result["final_thought"],
                    "tool_call": step_result["tool_call"].model_dump(),
                    "tool_result": step_result["tool_result"].model_dump(),
                }
                yield payload
                await asyncio.sleep(0.001)
            else:
                raise MaxIterationsExceeded(f"超过最大迭代次数 ({self.max_iterations})")

            assert result is not None
            new_trace = {
                "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "user": validated_input[:80],
                "steps": [s.model_dump(mode="json") for s in steps],
            } if steps else None
            await self._persist_session(
                sid, memory, prior_summary=prior_summary, wm=wm,
                prev_traces=prev_traces, new_trace=new_trace,
            )

            # 长期记忆：本轮完整对话后台异步写入（持引用防 GC）
            if self.long_term_memory is not None and self.long_term_memory.enabled:
                task = asyncio.create_task(
                    self.long_term_memory.add(
                        user_input=validated_input,
                        assistant_answer=result.answer,
                        session_id=sid,
                        user_id=user_id,
                    )
                )
                self._bg_tasks.add(task)
                task.add_done_callback(self._on_bg_done)

            yield {
                "type": "result",
                "result": result,
                "answer": result.answer,
                "total_duration_ms": result.total_duration_ms,
                "total_steps": len(result.steps),
                "total_tokens": total_tokens,
                "success": result.success,
            }

        except GuardrailError as e:
            logger.warning("Guardrail 拦截: %s", e)
            err = AgentResult(answer=str(e), steps=steps, success=False, error=str(e))
            yield {"type": "error", "result": err, "message": str(e)}
        except asyncio.CancelledError:
            # 客户端断开 / 用户点停止：把已发生的部分对话与工作记忆落盘，避免整轮白聊
            logger.info("会话 %s 执行被中断，保存部分状态", sid)
            try:
                await self._persist_session(
                    sid, memory, prior_summary=prior_summary, wm=wm,
                    prev_traces=prev_traces, new_trace=None,
                )
            except Exception as persist_err:
                logger.warning("中断落盘失败: %s", persist_err)
            raise
        except MaxIterationsExceeded as e:
            logger.warning("Agent 循环终止: %s", e)
            err = AgentResult(
                answer="抱歉，您的请求需要多次查询，请简化问题后重试或联系人工客服。",
                steps=steps, success=False, error=str(e),
            )
            yield {"type": "error", "result": err, "message": str(err.answer)}
        except Exception as e:
            logger.exception("Agent 执行异常")
            err = AgentResult(
                answer=f"系统异常: {e}", steps=steps, success=False, error=str(e)
            )
            yield {"type": "error", "result": err, "message": str(e)}

    def _account_budget(self, sid: str, wm: WorkingMemory, tokens: int) -> None:
        """会话级 token 累计与预算告警/硬停（主循环与工具重试共用）"""
        wm.tokens_used += tokens
        per = settings.token_budget_per_session
        alert_at = per * settings.token_budget_alert_ratio
        if wm.tokens_used >= per:
            if settings.token_budget_hard_stop:
                raise MaxIterationsExceeded(
                    f"本会话 token 用量已达预算上限（{wm.tokens_used}/{per}），"
                    "请开启新会话或联系管理员调整配置。"
                )
            if not getattr(wm, "_budget_warned", False):
                wm._budget_warned = True  # type: ignore[attr-defined]
                logger.warning(
                    "[ALERT][BUDGET] 会话 %s token 已超预算上限：%d/%d",
                    sid, wm.tokens_used, per,
                )
        elif wm.tokens_used >= alert_at and not getattr(wm, "_budget_alerted", False):
            wm._budget_alerted = True  # type: ignore[attr-defined]
            logger.warning(
                "[ALERT][BUDGET] 会话 %s token 用量达告警线：%d/%d",
                sid, wm.tokens_used, per,
            )

    # ── 会话持久化与压缩 ───────────────────────────────

    async def _persist_session(
        self,
        sid: str,
        memory: ShortTermMemory,
        *,
        prior_summary: str | None,
        wm: WorkingMemory,
        prev_traces: list[dict] | None = None,
        new_trace: dict | None = None,
    ) -> None:
        """落盘会话状态；超过阈值时把较旧的一半压缩为滚动摘要

        - LLM 看到的始终是「摘要 + 最近 keep_recent 条」（上下文工程核心）
        - 压缩失败自动降级为保留完整历史，绝不丢数据
        - traces 保留最近 8 轮推理轨迹，供前端历史回放
        """
        msgs = memory.all_messages()
        summary = prior_summary

        if (
            settings.context_compress_enabled
            and len(msgs) >= settings.context_compress_threshold
        ):
            keep = max(settings.context_keep_recent, 2)
            old_part, recent = msgs[:-keep], msgs[-keep:]
            new_summary = await self._summarize(prior_summary, old_part)
            if new_summary:
                summary = new_summary
                msgs = recent
                logger.info(
                    "会话 %s 已压缩: %d 条 → 摘要(%d字) + 保留最近 %d 条",
                    sid, len(old_part), len(summary), len(recent),
                )
            else:
                logger.info("会话 %s 压缩失败，保留完整历史 (%d 条)", sid, len(msgs))

        traces = list(prev_traces or [])
        if new_trace:
            traces.append(new_trace)
        traces = traces[-8:]

        await self.conversation_history.asave_state(
            sid, msgs, summary=summary, working_memory=wm.to_dict(), traces=traces,
            user_id=self._owner_uid,
        )

    async def _summarize(self, prior_summary: str | None, msgs: list[AgentMessage]) -> str | None:
        """LLM 滚动摘要：把较旧对话压缩成一段保留关键事实的短文"""
        try:
            transcript = "\n".join(f"{m.role.value}: {m.content}" for m in msgs)
            prompt = (
                "你是客服对话摘要助手。请把以下智能客服对话记录压缩为一段中文摘要，"
                f"不超过{settings.context_summary_max_chars}字。\n"
                "必须保留：用户表达的预算及品类、提到过的订单号/物流单号、"
                "已确认的结论与承诺、未解决的诉求。省略寒暄和工具调用细节，直接输出摘要正文。\n\n"
            )
            if prior_summary:
                prompt += f"[更早的已有摘要]\n{prior_summary}\n\n"
            prompt += f"[待压缩的对话记录]\n{transcript}"

            reply = await self.llm.chat_async(
                [
                    AgentMessage(role=ChatRole.system, content="你是对话摘要助手，只输出摘要本身。"),
                    AgentMessage(role=ChatRole.user, content=prompt),
                ],
                temperature=0.1,
            )
            text = reply.content.strip()
            return text[: settings.context_summary_max_chars] or None
        except Exception as e:
            logger.warning("会话摘要生成失败: %s", e)
            return None

    # ── 工具步骤执行 ───────────────────────────────────

    async def _execute_tool_step(
        self,
        sid: str,
        step_index: int,
        system_prompt: str,
        memory: ShortTermMemory,
        initial_thought: str,
        initial_call: ToolCall,
        req_metrics: MetricsCollector,
    ) -> dict[str, Any]:
        """执行一次「LLM 决定调工具 → 执行（含修正重试）→ 结果入记忆」的完整步骤"""
        thought = initial_thought
        tool_call = initial_call
        extra_tokens = 0
        max_r = settings.tool_max_retries
        retry_count = 0

        while True:
            try:
                tool = self.registry.get_tool(tool_call.tool_name)
                self._validate_tool_args(tool, tool_call.arguments)

                tool_start = time.perf_counter()
                output = await tool.run(**tool_call.arguments)
                tool_duration = (time.perf_counter() - tool_start) * 1000

                masked_output = self.guardrails.check_tool_output(str(output), session_id=sid)
                tool_result = ToolResult(
                    tool_call_id=tool_call.id,
                    tool_name=tool_call.tool_name,
                    success=True,
                    output=masked_output,
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
                    logger.warning(
                        "工具 %s 重试 %d 次后放弃: %s", tool_call.tool_name, retry_count - 1, e
                    )
                    tool_result = ToolResult(
                        tool_call_id=tool_call.id,
                        tool_name=tool_call.tool_name,
                        success=False,
                        output=str(e),
                    )
                    break

                logger.warning(
                    "工具 %s 执行失败(第%d次), 交由 LLM 修正: %s", tool_call.tool_name, retry_count, e
                )
                # 失败的 thought 一并写入，LLM 能看到自己上一步的动作
                memory.add(AgentMessage(role=ChatRole.assistant, content=thought))
                memory.add(AgentMessage(
                    role=ChatRole.tool,
                    content=f"[工具 {tool_call.tool_name}] 执行失败(重试{retry_count}/{max_r}): {e}",
                    tool_name=tool_call.tool_name,
                    tool_call_id=tool_call.id,
                ))

                messages = self._build_messages(system_prompt, memory)
                reply = await self.llm.chat_async(messages, temperature=settings.temperature)
                thought = reply.content
                tokens = reply.total_tokens or estimate_tokens(thought)
                self._record_llm(req_metrics, tokens)
                extra_tokens += tokens

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
                self._record_tool(req_metrics, tool_call.tool_name)
                continue

        # 思考与观察成对写入记忆，保持对话结构完整
        memory.add(AgentMessage(role=ChatRole.assistant, content=thought))
        memory.add(AgentMessage(
            role=ChatRole.tool,
            content=f"[工具 {tool_call.tool_name}] 返回: {tool_result.output}",
            tool_name=tool_call.tool_name,
            tool_call_id=tool_call.id,
        ))
        self._record_tool(req_metrics, tool_call.tool_name)

        record = StepRecord(
            step_index=step_index,
            thought=thought,
            tool_call=tool_call,
            tool_result=tool_result,
        )
        return {
            "record": record,
            "final_thought": thought,
            "tool_call": tool_call,
            "tool_result": tool_result,
            "extra_tokens": extra_tokens,
        }

    # ── 观测与工具函数 ─────────────────────────────────

    def _record_llm(self, req_metrics: MetricsCollector, tokens: int) -> None:
        req_metrics.record_llm_call(tokens)
        self.metrics.record_llm_call(tokens)  # 进程级聚合

    def _record_tool(self, req_metrics: MetricsCollector, tool_name: str) -> None:
        req_metrics.record_tool_call(tool_name)
        self.metrics.record_tool_call(tool_name)

    def _record_duration(self, req_metrics: MetricsCollector, ms: float) -> None:
        req_metrics.record_duration(ms)
        self.metrics.record_duration(ms)

    def _on_bg_done(self, task: asyncio.Task) -> None:
        self._bg_tasks.discard(task)
        if not task.cancelled() and task.exception() is not None:
            logger.warning("长期记忆后台写入失败: %s", task.exception())

    @staticmethod
    def _build_messages(system_prompt: str, memory: ShortTermMemory) -> list[AgentMessage]:
        return [
            AgentMessage(role=ChatRole.system, content=system_prompt),
            *memory.get_context(),
        ]

    @staticmethod
    def _generate_id() -> str:
        import hashlib
        import uuid

        return hashlib.md5(uuid.uuid4().bytes).hexdigest()[:12]

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
        except (json.JSONDecodeError, KeyError, TypeError) as e:
            logger.warning(
                "解析工具调用失败: %s | text=%s", e, text[match.end(): match.end() + 80]
            )
            return None

    @staticmethod
    def _looks_like_action(text: str) -> bool:
        """文本声称要调工具（含 ACTION 关键字），但 JSON 可能没写对"""
        return bool(re.search(r"ACTION\s*:", text))

    @staticmethod
    def _validate_tool_args(tool: BaseTool, args: dict[str, Any]) -> None:
        required = tool.spec.parameters.get("required", [])
        for key in required:
            value = args.get(key)
            if key not in args or (isinstance(value, str) and not value.strip()):
                raise ToolError(f"工具 '{tool.spec.name}' 缺少必填参数: {key}")
