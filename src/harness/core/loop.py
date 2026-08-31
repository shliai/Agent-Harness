from __future__ import annotations

import asyncio
import json
import logging
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
from harness.memory.learning import (
    LearningRecord,
    LearningStore,
)
from harness.memory.short_term import ShortTermMemory
from harness.memory.working_memory import WorkingMemory
from harness.observability.metrics import MetricsCollector
from harness.observability.tracer import Tracer
from harness.tools.base import BaseTool

logger = logging.getLogger("harness.core.loop")

# 终态 / 提案工具：承载「每轮必须输出工具」约束下的回复与人工确认环节。
# respond=最终回复，plan=先问用户/需确认（不直接执行）。
from harness.core.intents import (
    FINALIZE_TOOL,
    PLAN_TOOL,
    _TRIVIAL_INPUT_RE,
    _PRODUCT_INTENT_RE,
    _ANAPHORA_RE,
    _AFTERSALE_PROGRESS_RE,
    _AFTERSALE_INTENT_RE,
    _LOGISTICS_HINT_RE,
    _ORDER_STATUS_RE,
    _NEW_ORDER_ID_RE,
    _NEW_TRACKING_RE,
    _EXPR_RE,
    _CALC_RE,
    _COMPLAINT_RE,
    _POLICY_FEAS_RE,
    _MY_ORDERS_RE,
    _BUNDLE_INTENT_RE,
    _CLARIFY_RE,
    _plan_forced_readonly,
    _build_forced_query,
    estimate_tokens,
)
from harness.core.prompts import (
    SYSTEM_PROMPT_TEMPLATE,
    ROLLING_SUMMARY_MAX_CHARS,
    FOLD_SYSTEM_PROMPT,
)

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
        learning_store: LearningStore | None = None,
        cheap_llm: AbstractLLMClient | None = None,
    ) -> None:
        self.llm = llm
        # 小模型（可空）：旁路低风险调用（事实抽取等）优先走它，省成本；
        # 未配置时回退主模型，行为与旧版一致
        self.cheap_llm = cheap_llm
        self.registry = registry
        self.guardrails = guardrails
        self.tracer = tracer
        self.metrics = metrics  # 进程级聚合指标（只累加，不 reset）
        self.conversation_history = conversation_history
        self.max_iterations = max_iterations
        self.learning_store = learning_store
        self._bg_tasks: set[asyncio.Task] = set()  # 后台任务持引用，防 GC
        # 轮末记忆整理的后台落盘任务（按 session 登记）：下一轮开始前 await，
        # 保证读到完整状态；同时让 [DONE] 随 result 立即送达，不再占用户感知时间
        # 注意：本类可能以单例被并发调用，严禁挂任何跨请求可变实例状态——
        # 身份/会话信息一律以局部变量与事件载荷传递。
        self._pending_finalize: dict[str, asyncio.Task] = {}
        # 每轮请求内的护栏计数器（run() 开头重置，避免单例跨请求污染）
        self._stuck_count = 0
        self._last_call_key: str | None = None
        self._tool_calls_total = 0

    # ── 原生 function calling（唯一工具调用协议）──────────

    def _native_tools(self) -> list[dict]:
        """OpenAI tools 载荷；注册中心为空时返回空列表（不携带 tools 参数）"""
        return self.registry.get_openai_tools()

    @staticmethod
    def _to_native_messages(messages: list[AgentMessage]) -> list[AgentMessage]:
        """无状态原生调用变换：历史 tool 角色消息改写为 user 文本。

        历史中的 assistant 消息不含 tool_calls 字段，严格校验的服务端会拒绝
        「无配对的 role=tool」消息；把工具结果平铺成 user 文本可兼容所有
        OpenAI v1 端点，且不损失任何信息（每轮都是独立无状态规划）。
        OBSERVATION: 前缀让历史观察与模型自发文本形态可区分，
        降低弱模型仿写 `[工具 …返回]` 伪造观察的面。
        """
        out: list[AgentMessage] = []
        for m in messages:
            if m.role == ChatRole.tool:
                name = m.tool_name or "tool"
                out.append(AgentMessage(
                    role=ChatRole.user, content=f"OBSERVATION: [工具 {name} 返回] {m.content}"))
            else:
                out.append(m)
        return out

    async def execute(
        self, user_input: str, session_id: str | None = None, user_id: str | None = None
    ) -> AgentResult:
        """非流式执行：消费 execute_stream 并返回最终结果"""
        result: AgentResult | None = None
        stream_sid: str | None = None
        async for event in self.execute_stream(user_input, session_id=session_id, user_id=user_id):
            if event["type"] == "result" or event["type"] == "error":
                result = event["result"]
                # 未显式传入 session_id 时，从事件载荷获取本次执行实际生成的会话 id
                stream_sid = event.get("session_id") or stream_sid
        if result is None:
            raise MaxIterationsExceeded("Agent 未产生任何结果")
        # 非流式调用方（CLI/测试）需拿到完整落盘状态：等待本会话的后台收尾任务
        pending = self._pending_finalize.get(stream_sid) if stream_sid else None
        if pending:
            try:
                await pending
            except Exception:
                logger.warning("等待会话落盘任务失败: %s", stream_sid)
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
        # uid 为本次请求局部变量：会话归属（落盘/越权校验）均由此闭包传递，
        # 不写入实例属性——单例并发下跨请求互不可见
        # 请求级身份上下文：订单归属校验 / 我的订单 / 工单归属 都从这里取
        current_user_id.set(uid)
        current_session_id.set(sid)
        # 每请求清空 calculator 缓存，避免跨轮误复用
        if hasattr(self, "_calc_cache"):
            self._calc_cache.clear()
        memory = ShortTermMemory(track_full=True)

        # 上一轮的轮末落盘可能仍在后台运行：先等它完成再加载状态，避免读到过期状态
        pending = self._pending_finalize.get(sid)
        if pending:
            try:
                await pending
            except Exception:
                logger.warning("等待上一轮落盘任务失败，按现有状态继续")

        # 恢复会话完整状态：消息全量 + 冻结章节 + 工作记忆槽位 + 历史推理轨迹
        chapters: list[str] = []
        prev_traces: list[dict] = []
        wm = WorkingMemory()
        if session_id:
            state = await self.conversation_history.aload_state(session_id)
            if state:
                chapters = list(state.get("chapters") or [])
                for raw_msg in state.get("messages", []):
                    try:
                        memory.add(AgentMessage(**raw_msg))
                    except Exception:
                        logger.warning("跳过无法解析的历史消息: %s", str(raw_msg)[:80])
                prev_traces = list(state.get("traces") or [])[-8:]
                wm = WorkingMemory.from_dict(state.get("working_memory"))
                logger.info(
                    "已恢复会话 %s: %d 条历史消息, %d 个冻结章节",
                    session_id, len(memory.all_messages()), len(chapters),
                )

        # 预算槽位供检索工具确定性注入价格条件；用量槽供流式回写真实 usage
        from harness.tools.context import current_budget, llm_usage_sink

        current_budget.set(wm.budget_amount)
        _llm_usage_sink_var = llm_usage_sink

        try:
            validated_input = self.guardrails.check_input(user_input, session_id=sid)

            # 工具以「一行职责简述」内联进提示词（弱模型可见性），
            # 参数 schema 由 tools 载荷原生携带，不再渲染 dict repr
            system_prompt = SYSTEM_PROMPT_TEMPLATE.format(
                tool_list=self.registry.get_tool_briefs() or "（当前没有可用工具）"
            )

            # 学习机制：单用户确定性学习画像，全量注入系统提示词（无向量检索、无每轮嵌入）
            if self.learning_store is not None and self.learning_store.enabled:
                lb = self.learning_store.render_for_prompt()
                if lb:
                    system_prompt = system_prompt + "\n\n" + lb

            # 上下文工程：跨轮工作记忆（预算/订单号/物流号，确定性规则维护）
            # 轮次 = 历史用户消息数 + 1（消息数组含 assistant/tool，不能直接当轮数）
            turn_no = sum(
                1 for m in memory.all_messages() if m.role == ChatRole.user
            ) + 1
            wm.update_from_input(validated_input, turn=turn_no)

            # 状态尾注：仅当期工作记忆（预算/订单号/物流号，确定性规则维护）。
            # 长期学习画像已在系统提示词中注入（稳定、跨轮复用，无每轮检索）。
            state_parts: list[str] = []

            wm_block = wm.prompt_block()
            if wm_block:
                state_parts.append(wm_block)

            state_note = "\n\n".join(state_parts) or None

            memory.add(AgentMessage(role=ChatRole.user, content=validated_input))

            result: AgentResult | None = None

            # 重置每轮护栏计数器（Agent 实例可能被多请求复用）
            self._stuck_count = 0
            self._last_call_key = None
            self._tool_calls_total = 0
            self._transfer_human_count = 0

            for step_index in range(self.max_iterations):
                # 首轮前置拦截（确定性护栏）：模型输出前先做两类强制调用，
                # 避免流式输出"凭记忆"内容再被回滚：
                #   a) 商品意图 → 强制 knowledge_retrieval（防幻觉）
                #   b) 指代/进度问法 + 记忆有实体 → 强制对应只读查询工具
                forced_call: ToolCall | None = None
                forced_thought = ""
                if step_index == 0:
                    if (
                        "knowledge_retrieval" in self.registry.list_tools()
                        and _PRODUCT_INTENT_RE.search(validated_input)
                        and not _BUNDLE_INTENT_RE.search(validated_input)
                        and not (
                            _POLICY_FEAS_RE.search(validated_input)
                            or _COMPLAINT_RE.search(validated_input)
                            or _CALC_RE.search(validated_input)
                        )
                    ):
                        logger.info(
                            "会话 %s 商品意图前置强制检索: %s", sid, validated_input[:40]
                        )
                        yield {"type": "delta_reset", "reason": "forced_retrieval"}
                        forced_call = ToolCall(
                            tool_name="knowledge_retrieval",
                            arguments={"query": _build_forced_query(validated_input, wm)},
                        )
                        forced_thought = "用户问题涉及具体商品，先调用 knowledge_retrieval 获取权威数据再回答。"
                    else:
                        readonly = _plan_forced_readonly(validated_input, wm)
                        if readonly is not None:
                            tool_name, arguments = readonly
                            # plan 工具特殊：触发提案模式而非直接执行
                            if tool_name == PLAN_TOOL:
                                actions = arguments.get("actions") or []
                                message = arguments.get("message") or ""
                                wm.set_pending_plan(actions, message)
                                logger.info(
                                    "会话 %s 前置强制 plan 提案: %s",
                                    sid, message[:40],
                                )
                                yield {"type": "delta_reset", "reason": "forced_retrieval"}
                                result, rec, filtered, raw = self._finalize_answer(
                                    message, step_index=step_index, parsed=[], memory=memory,
                                    req_metrics=req_metrics, steps=steps, total_tokens=total_tokens,
                                    start_time=start_time, sid=sid, wm=wm, tool_name=PLAN_TOOL,
                                )
                                if filtered != raw:
                                    yield {"type": "answer_replace", "content": filtered}
                                yield {
                                    "type": "step", "step_index": step_index, "thought": rec.thought,
                                    "tool_call": None, "tool_result": None, "final": True,
                                }
                                break
                            logger.info(
                                "会话 %s 指代追问前置强制查询 %s: %s",
                                sid, tool_name, validated_input[:40],
                            )
                            yield {"type": "delta_reset", "reason": "forced_retrieval"}
                            forced_call = ToolCall(tool_name=tool_name, arguments=arguments)
                            forced_thought = (
                                f"用户在追问上文提到的事项，先调用 {tool_name} 获取最新数据再回答。"
                            )

                if forced_call is not None:
                    step_result = await self._execute_tool_step(
                        sid=sid,
                        step_index=step_index,
                        system_prompt=system_prompt,
                        memory=memory,
                        initial_thought=forced_thought,
                        initial_call=forced_call,
                        req_metrics=req_metrics,
                        chapters=chapters,
                        state_note=state_note,
                    )
                    total_tokens += step_result["extra_tokens"]
                    self._account_budget(sid, wm, step_result["extra_tokens"])
                    steps.append(step_result["record"])
                    self.tracer.record_step(
                        step_index, step_result["final_thought"],
                        step_result["tool_call"], step_result["tool_result"], session_id=sid,
                    )
                    yield {
                        "type": "step",
                        "step_index": step_index,
                        "thought": step_result["final_thought"],
                        "tool_call": step_result["tool_call"].model_dump(),
                        "tool_result": step_result["tool_result"].model_dump(),
                    }
                    await asyncio.sleep(0.001)
                    continue
                messages = self._build_messages(system_prompt, memory, chapters, state_note)

                # token 级流式：增量即时推送前端；若本轮实际是工具规划，
                # 由 delta_reset 事件通知前端回滚临时文本。
                # 受 tool_choice=required 约束，模型每轮都必须产出非空 tool_call 列表。
                buf: list[str] = []
                usage_sink: dict = {}
                tc_sink: dict = {}
                _llm_usage_sink_var.set(usage_sink)

                tools = self._native_tools()
                call_kwargs: dict = {"temperature": settings.temperature}
                call_messages = messages
                if tools:
                    call_kwargs["tools"] = tools
                    call_kwargs["tool_choice"] = settings.agent_tool_choice
                    call_kwargs["tool_call_sink"] = tc_sink
                    call_messages = self._to_native_messages(messages)
                async for delta in self.llm.stream_chat_async(call_messages, **call_kwargs):
                    buf.append(delta)
                    yield {"type": "delta", "content": delta}
                thought = "".join(buf).strip()

                # 统计口径（单一事实源）：优先供应商真实 usage（prompt+completion 双侧），
                # 只有流式未返回 usage 时才退回 estimate_tokens 启发式。
                # wm.tokens_used 与压缩触发闸门都由 _account_budget 统一累计，
                # 保证预算告警线与压缩线读的是同一个数
                real = usage_sink.get("total") or 0
                if real:
                    tokens = real
                else:
                    prompt_est = estimate_tokens("".join(m.content for m in messages))
                    tokens = prompt_est + estimate_tokens(thought)
                self._record_llm(req_metrics, tokens)
                total_tokens += tokens

                self._account_budget(sid, wm, tokens)

                # 工具调用解析：仅接受原生结构化 tool_calls；无调用即视为最终回答。
                # 原生不支持 required 的端点若退回纯文本，按最终回复兜底（不崩溃）。
                native_calls = tc_sink.get("tool_calls") or []
                if not native_calls:
                    # ── 兜底：端点的 required 被忽略，返回纯文本 → 当作最终回复 ──
                    filtered = self.guardrails.check_output(thought, session_id=sid)
                    if not filtered.strip():
                        filtered = "（本次未产生有效回复，请换个问法或稍后再试。）"
                    if filtered != thought:
                        yield {"type": "answer_replace", "content": filtered}
                    memory.add(AgentMessage(role=ChatRole.assistant, content=filtered))
                    self.tracer.record_step(step_index, thought, None, None, session_id=sid)
                    steps.append(StepRecord(step_index=step_index, thought=thought, round_index=step_index))
                    yield {
                        "type": "step",
                        "step_index": step_index,
                        "thought": thought,
                        "final": True,
                    }
                    await asyncio.sleep(0.001)
                    duration = (time.perf_counter() - start_time) * 1000
                    self._record_duration(req_metrics, duration)
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

                # 解析全部工具调用（任务列表式 ReAct：一轮可产出多个工具）
                parsed: list[ToolCall] = []
                for nc in native_calls:
                    thought = thought or f"调用工具 {nc['name']}"
                    parsed.append(ToolCall(tool_name=nc["name"], arguments=nc["arguments"]))

                # 死循环护栏：连续相同「工具名」且无新结果 → 强制终态
                # knowledge_retrieval 例外：正常业务会多次调用不同查询，不计入 stuck
                domain_tool_names = [
                    c.tool_name for c in parsed if c.tool_name != "knowledge_retrieval"
                ]
                call_key = tuple(sorted(domain_tool_names)) if domain_tool_names else ("__none__",)
                if call_key == self._last_call_key:
                    self._stuck_count += 1
                else:
                    self._stuck_count = 0
                    self._last_call_key = call_key
                if self._stuck_count >= settings.agent_stuck_threshold:
                    result, _rec, _f, _r = self._finalize_answer(
                        "（已多次尝试相同操作仍无新进展，为避免死循环已停止；请补充更明确的信息或参数后继续。）",
                        step_index=step_index, parsed=parsed, memory=memory,
                        req_metrics=req_metrics, steps=steps, total_tokens=total_tokens,
                        start_time=start_time, sid=sid, wm=wm,
                    )
                    yield {"type": "step", "step_index": step_index, "thought": _f, "final": True}
                    break

                call_names = {c.tool_name for c in parsed}

                # ── PROPOSE 模式：plan 提案待确认，不直接执行 ──
                if PLAN_TOOL in call_names:
                    plan_call = next(c for c in parsed if c.tool_name == PLAN_TOOL)
                    actions = plan_call.arguments.get("actions") or []
                    message = plan_call.arguments.get("message") or ""
                    wm.set_pending_plan(actions, message)
                    result, rec, filtered, raw = self._finalize_answer(
                        message, step_index=step_index, parsed=[plan_call], memory=memory,
                        req_metrics=req_metrics, steps=steps, total_tokens=total_tokens,
                        start_time=start_time, sid=sid, wm=wm, tool_name=PLAN_TOOL,
                    )
                    if filtered != raw:
                        yield {"type": "answer_replace", "content": filtered}
                    yield {
                        "type": "step", "step_index": step_index, "thought": rec.thought,
                        "tool_call": rec.tool_call.model_dump(),
                        "tool_result": rec.tool_result.model_dump(), "final": True,
                    }
                    break

                # ── 执行/终态拆分：领域工具先顺序执行；respond 仅承载最终回复 ──
                domain_calls = [c for c in parsed if c.tool_name not in (PLAN_TOOL, FINALIZE_TOOL)]
                finalize_calls = [c for c in parsed if c.tool_name == FINALIZE_TOOL]

                if not domain_calls:
                    # 仅 respond（或无工具）→ ANSWER 终态
                    content = finalize_calls[0].arguments.get("content") or "" if finalize_calls else thought
                    result, rec, filtered, raw = self._finalize_answer(
                        content, step_index=step_index,
                        parsed=[finalize_calls[0]] if finalize_calls else [],
                        memory=memory, req_metrics=req_metrics, steps=steps,
                        total_tokens=total_tokens, start_time=start_time, sid=sid, wm=wm,
                        tool_name=FINALIZE_TOOL if finalize_calls else None,
                    )
                    wm.clear_pending_plan()
                    if filtered != raw:
                        yield {"type": "answer_replace", "content": filtered}
                    yield {
                        "type": "step", "step_index": step_index, "thought": rec.thought,
                        "tool_call": rec.tool_call.model_dump() if rec.tool_call else None,
                        "tool_result": rec.tool_result.model_dump() if rec.tool_result else None,
                        "final": True,
                    }
                    break

                # ── EXECUTE 模式：顺序执行领域工具列表 ──
                budget_hit = False
                for tc in domain_calls:
                    if tc.tool_name == "transfer_human":
                        self._transfer_human_count += 1
                        if self._transfer_human_count > 1:
                            result, rec, _f, _r = self._finalize_answer(
                                "（已转人工，请等待人工客服接入。）",
                                step_index=step_index, parsed=[tc], memory=memory,
                                req_metrics=req_metrics, steps=steps, total_tokens=total_tokens,
                                start_time=start_time, sid=sid, wm=wm,
                            )
                            yield {"type": "step", "step_index": step_index, "thought": _f, "final": True}
                            budget_hit = True
                            break
                    self._tool_calls_total += 1
                    if self._tool_calls_total > settings.agent_tool_budget:
                        result, rec, _f, _r = self._finalize_answer(
                            "（已达到本轮工具调用上限，为避免失控已停止；请补充信息后继续。）",
                            step_index=step_index, parsed=[tc], memory=memory,
                            req_metrics=req_metrics, steps=steps, total_tokens=total_tokens,
                            start_time=start_time, sid=sid, wm=wm,
                        )
                        yield {"type": "step", "step_index": step_index, "thought": _f, "final": True}
                        budget_hit = True
                        break
                    step_result = await self._execute_tool_step(
                        sid=sid,
                        step_index=step_index,
                        system_prompt=system_prompt,
                        memory=memory,
                        initial_thought=thought,
                        initial_call=tc,
                        req_metrics=req_metrics,
                        chapters=chapters,
                        state_note=state_note,
                    )
                    total_tokens += step_result["extra_tokens"]
                    self._account_budget(sid, wm, step_result["extra_tokens"])
                    steps.append(step_result["record"])
                    self.tracer.record_step(
                        step_index, step_result["final_thought"],
                        step_result["tool_call"], step_result["tool_result"], session_id=sid,
                    )
                    # 修正重试发生在步骤内部（无流式输出），推事件让前端
                    # 显示「修正中」而非静默卡顿；下一轮 delta_reset 前先清残留文本
                    if step_result.get("retried"):
                        yield {"type": "delta_reset", "reason": "tool_retry"}
                    yield {
                        "type": "step",
                        "step_index": step_index,
                        "round_index": step_index,
                        "thought": step_result["final_thought"],
                        "tool_call": step_result["tool_call"].model_dump(),
                        "tool_result": step_result["tool_result"].model_dump(),
                    }
                    await asyncio.sleep(0.001)
                if budget_hit:
                    break
                # 本轮工具执行完：同轮含 respond 则以其内容终态，否则进入下一轮推理
                if finalize_calls:
                    content = finalize_calls[0].arguments.get("content") or ""
                    result, rec, filtered, raw = self._finalize_answer(
                        content, step_index=step_index, parsed=[finalize_calls[0]],
                        memory=memory, req_metrics=req_metrics, steps=steps,
                        total_tokens=total_tokens, start_time=start_time, sid=sid, wm=wm,
                        tool_name=FINALIZE_TOOL,
                    )
                    wm.clear_pending_plan()
                    if filtered != raw:
                        yield {"type": "answer_replace", "content": filtered}
                    yield {
                        "type": "step", "step_index": step_index, "thought": rec.thought,
                        "tool_call": rec.tool_call.model_dump(),
                        "tool_result": rec.tool_result.model_dump(), "final": True,
                    }
                    break
                wm.clear_pending_plan()
                continue
            else:
                raise MaxIterationsExceeded(f"超过最大迭代次数 ({self.max_iterations})")

            assert result is not None

            # 答案先行推给用户——之后的记忆整理不占用用户感知时间
            yield {
                "type": "result",
                "result": result,
                "answer": result.answer,
                "session_id": sid,
                "total_duration_ms": result.total_duration_ms,
                "total_steps": len(result.steps),
                "total_tokens": total_tokens,
                "success": result.success,
            }

            # ── 记忆整理（后台任务：答案已送达，[DONE] 立即返回，UI 即时脱离推理态）──
            # 确定性捕获偏好/约束/纠正 → 合并进学习画像(JSON) + 会话落盘；
            # 整块放入 asyncio.create_task，生成器在此立即 return → SSE 关闭 → 前端 finally 收尾。
            # 数据一致性：任务按 session 登记到 _pending_finalize，下一轮开始前 await 它，
            # 因此紧接的下一条消息能读到完整状态；学习画像延迟写入对单会话用法无影响。
            #
            # 捕获原则：仅来自确定性信号（预算槽位/显式偏好/硬约束/纠正表达），不调 LLM 自由抽取。
            # uid 是本次请求的局部变量，闭包捕获即快照，不受并发请求影响。

            async def _finalize() -> None:
                try:
                    # ── 折叠式滚动摘要：cheap_llm 把「旧进展+本轮问答」压成有界备忘录 ──
                    # 门控：寒暄/失败轮/过短回答不调模型；小模型不可用或失败沿用旧摘要
                    if (result.success
                            and not _TRIVIAL_INPUT_RE.match(validated_input.strip())
                            and len(result.answer.strip()) >= 12
                            and self.cheap_llm is not None):
                        try:
                            fold_prompt = (
                                f"【旧进展】{wm.rolling_summary or '（无，本轮是会话开头）'}\n\n"
                                f"【用户】{validated_input[:500]}\n"
                                f"【客服】{result.answer[:800]}"
                            )
                            reply = await self.cheap_llm.chat_async(
                                [
                                    AgentMessage(role=ChatRole.system, content=FOLD_SYSTEM_PROMPT),
                                    AgentMessage(role=ChatRole.user, content=fold_prompt),
                                ],
                                temperature=0.1,
                            )
                            folded = reply.content.strip()[:ROLLING_SUMMARY_MAX_CHARS]
                            if folded:
                                wm.rolling_summary = folded
                                logger.info("轮末滚动摘要已折叠 (%d 字)", len(folded))
                        except Exception as e:
                            logger.warning("滚动摘要折叠失败，沿用旧值: %s", e)

                    # 学习机制：仅从确定性信号捕获偏好/约束/纠正（不调 LLM 自由抽取）
                    if (self.learning_store is not None
                            and self.learning_store.enabled
                            and result.success
                            and not _TRIVIAL_INPUT_RE.match(validated_input.strip())):
                        ts = time.strftime("%Y-%m-%dT%H:%M:%S")
                        recs: list[LearningRecord] = []
                        # 长期记忆直接读取工作记忆（单点确定性抽取，无二次抽取）
                        for sig_type, sig_key, sig_val in wm.learning_signals():
                            recs.append(LearningRecord(
                                type=sig_type, key=sig_key, value=sig_val,
                                evidence=validated_input[:80], ts=ts))
                        for r in recs:
                            self.learning_store.add(r)
                        if recs:
                            logger.info("轮末学习 %d 条（来自工作记忆）", len(recs))

                    new_trace = {
                        "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
                        "user": validated_input[:80],
                        "steps": [s.model_dump(mode="json") for s in steps],
                    } if steps else None
                    await self._persist_session(
                        sid, memory, chapters=chapters, wm=wm,
                        prev_traces=prev_traces, new_trace=new_trace,
                        user_id=uid,
                    )
                except Exception:
                    logger.exception("轮末记忆整理后台任务失败")

            task = asyncio.create_task(_finalize())
            self._pending_finalize[sid] = task
            task.add_done_callback(lambda t: self._pending_finalize.pop(sid, None))

        except GuardrailError as e:
            logger.warning("Guardrail 拦截: %s", e)
            err = AgentResult(answer=str(e), steps=steps, success=False, error=str(e))
            yield {"type": "error", "result": err, "message": str(e), "session_id": sid}
        except asyncio.CancelledError:
            # 客户端断开 / 用户点停止：把已发生的部分对话与工作记忆落盘，避免整轮白聊
            logger.info("会话 %s 执行被中断，保存部分状态", sid)
            try:
                await self._persist_session(
                    sid, memory, chapters=chapters, wm=wm,
                    prev_traces=prev_traces, new_trace=None,
                    user_id=uid,
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
            yield {"type": "error", "result": err, "message": str(err.answer), "session_id": sid}
        except Exception as e:
            logger.exception("Agent 执行异常")
            err = AgentResult(
                answer=f"系统异常: {e}", steps=steps, success=False, error=str(e)
            )
            yield {"type": "error", "result": err, "message": str(e), "session_id": sid}

    def _account_budget(self, sid: str, wm: WorkingMemory, tokens: int) -> None:
        """会话级 token 计量与预算告警/硬停（主循环与工具重试共用）

        - tokens_used：累计总量，优先供应商真实 usage、估算仅回退，
          预算告警/硬停与轮末 token 统计共用此口径；
        - 压缩触发另算「当前窗口消息占用」（见 _persist_session），两者语义不同。
        告警标志持久化到 WorkingMemory 字段，重启不重复告警。"""
        wm.tokens_used += tokens
        per = settings.token_budget_per_session
        alert_at = per * settings.token_budget_alert_ratio
        if wm.tokens_used >= per:
            if settings.token_budget_hard_stop:
                raise MaxIterationsExceeded(
                    f"本会话 token 用量已达预算上限（{wm.tokens_used}/{per}），"
                    "请开启新会话或联系管理员调整配置。"
                )
            if not wm.budget_warned:
                wm.budget_warned = True
                logger.warning(
                    "[ALERT][BUDGET] 会话 %s token 已超预算上限：%d/%d",
                    sid, wm.tokens_used, per,
                )
        elif wm.tokens_used >= alert_at and not wm.budget_alerted:
            wm.budget_alerted = True
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
        chapters: list[str],
        wm: WorkingMemory,
        prev_traces: list[dict] | None = None,
        new_trace: dict | None = None,
        user_id: str | None = None,
    ) -> None:
        """落盘会话状态；达到压缩触发条件时把本周期压缩为一个冻结章节

        - LSM 式章节追加：摘要 + WM 快照组装成新章节 append 到 chapters，
          旧章节永不改写——压缩事件只让历史区位移一次，已缓存章节保持命中
        - 章节烘焙后工作记忆清零，新周期从空白积累
        - 压缩失败自动降级为保留完整历史，绝不丢数据
        - traces 保留最近 8 轮推理轨迹，供前端历史回放
        """
        msgs = memory.all_messages()

        # 压缩触发口径 = 「当前窗口消息占用」：估算当前待发送消息的 token，
        # 与窗口×比例比较。故意不用累计 usage——累计值含大量已完成轮次的
        # 消耗与不变的系统提示词，跨周期保留会导致压缩后立刻再触发。
        # 启发式对中文误差 ±30%，触发线已留 25% 余量兜底
        if (
            settings.context_compress_enabled
            and msgs
            and estimate_tokens("\n".join(m.content for m in msgs))
            >= settings.context_window_tokens * settings.context_compress_ratio
        ):
            old_part, recent = memory.split_for_compression(settings.context_keep_recent)
            cycle_no = len(chapters) + 1
            prev_chapter = chapters[-1] if chapters else None
            chapter_summary = await self._summarize(old_part, prev_chapter)
            if chapter_summary:
                # 组装冻结章节：本周期压缩内容 + 该周期结束时的 WM 硬实体快照
                # （for_archive：只归档跨周期仍生效的约束/单号，进行时态信息作废）
                snapshot = wm.prompt_block(for_archive=True) or "（本周期无显著任务状态）"
                chapters.append(
                    f"【第{cycle_no}阶段】对话压缩\n{chapter_summary}\n\n"
                    f"【该阶段任务状态】\n{snapshot}"
                )
                msgs = recent
                memory.trim_to(recent)
                wm.reset_for_new_cycle()
                logger.info(
                    "会话 %s 已压缩为第%d章节: %d 条 → 摘要(%d字)+WM快照, 保留最近 %d 条",
                    sid, cycle_no, len(old_part), len(chapter_summary), len(recent),
                )
            else:
                logger.info("会话 %s 压缩失败，保留完整历史 (%d 条)", sid, len(msgs))

        traces = list(prev_traces or [])
        if new_trace:
            traces.append(new_trace)
        traces = traces[-8:]

        await self.conversation_history.asave_state(
            sid, msgs, working_memory=wm.to_dict(), traces=traces,
            chapters=chapters, user_id=user_id or "",
        )

    # 压缩摘要的 system 提示词（模块级常量，测试以此识别摘要调用）
    SUMMARY_SYSTEM_PROMPT = (
        "你是电商客服 Agent 的对话档案员。你输出的摘要会作为独立上下文块注入后续对话，"
        "是客服了解该用户历史的唯一依据。只输出摘要正文，"
        "不要任何解释、寒暄或代码块标记。"
    )



    @staticmethod
    def _build_summary_prompt(transcript: str, prev_chapter: str | None = None) -> str:
        max_chars = settings.context_summary_max_chars
        anchor = ""
        if prev_chapter:
            # 跨章接续：喂入上一章内容作参照，保持实体锚点与叙事连贯（该章已归档，不再改动）
            anchor = (
                "## 上一阶段归档（仅供衔接参照，禁止重复其内容或改写它）\n"
                f"{prev_chapter[:800]}\n\n"
            )
        return (
            "请把以下对话记录压缩为一份阶段摘要（章节式记忆，每章独立、互不合并）。\n\n"
            + anchor +
            "## 核心要求：提炼，不是摘录\n"
            "- 用档案语言重写：每条信息是一行客观事实陈述，禁止对话体\n"
            "- 禁止出现\"用户说\"\"客服回答\"\"然后\"等引用或叙事结构，禁止按时间顺序复述过程\n"
            "- 合并同类信息：多轮讨论的同一件事只留最终结论与当前状态\n"
            "- 唯一例外：订单号/金额等原子标识符必须与原文完全一致——"
            "这是数据完整性要求（改一个数字就是另一个订单），不等于允许照搬句子\n\n"
            "## 指代锚点（关键）\n"
            "- 「关键标识符」必须写明实体对应关系，如：订单20240601001=小米17 Pro退货；\n"
            "  后续用户说\"那个订单/那款\"时，客服全靠这行解析指代\n"
            "- 用户明确拒绝过的推荐必须留痕，防止重复推销\n\n"
            "## 长度不足时的取舍顺序\n"
            "先删寒暄与重复追问的过程描述 → 再删已办结事项的细节；\n"
            "标识符、承诺数字、未解决事项永不删。\n\n"
            "## 摘要格式\n"
            f"总长度不超过 {max_chars} 字。用以下固定字段组织，"
            "没有内容的字段整行省略，字段顺序保持不变：\n\n"
            "【当前诉求】用户此刻最想解决的一件事\n"
            "【关键标识符】订单号/售后单号/物流单号/商品编号及其对应事项（逐字保真）\n"
            "【金额与预算】预算上限、涉事金额、优惠规则（数字保真）\n"
            "【办理进度】每个在办事项的状态机位置，如\"订单20240601001：退货申请待商家审核\"\n"
            "【已做承诺】客服已答应的事项与数字（到账天数、运费承担等）——合规凭据，数字保真\n"
            "【未解决事项】尚未办结的问题\n"
            "【已排除选项】用户拒绝过的方案或不满意的推荐\n"
            "【用户状态】情绪信号：不满、焦虑、重复追问、转人工倾向\n\n"
            "只输出摘要正文本身。\n\n"
            f"[待压缩的本周期对话记录]\n{transcript}"
        )

    async def _summarize(self, msgs: list[AgentMessage], prev_chapter: str | None = None) -> str | None:
        """LLM 章节压缩：把本周期旧对话按电商客服档案结构压缩成独立章节摘要。

        优先走 cheap_llm（低风险旁路任务），不可用/失败降级主模型；
        两级都失败返回 None，由调用方降级为保留完整历史（绝不丢数据）。"""
        transcript = "\n".join(f"{m.role.value}: {m.content}" for m in msgs)
        prompt = self._build_summary_prompt(transcript, prev_chapter)
        payload = [
            AgentMessage(role=ChatRole.system, content=self.SUMMARY_SYSTEM_PROMPT),
            AgentMessage(role=ChatRole.user, content=prompt),
        ]
        clients: list[AbstractLLMClient] = []
        if self.cheap_llm is not None:
            clients.append(self.cheap_llm)
        clients.append(self.llm)

        for client in clients:
            try:
                reply = await client.chat_async(payload, temperature=0.1)
                text = reply.content.strip()
                return text[: settings.context_summary_max_chars] or None
            except Exception as e:
                logger.warning("章节摘要生成失败（%s）: %s",
                               type(client).__name__, e)
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
        chapters: list[str] | None = None,
        state_note: str | None = None,
    ) -> dict[str, Any]:
        """执行一次「LLM 决定调工具 → 执行（含修正重试）→ 结果入记忆」的完整步骤"""
        # calculator 同参数去重：同一轮同表达式只执行一次，直接复用结果
        if initial_call.tool_name == "calculator":
            expr = str(initial_call.arguments.get("expression", "")).strip()
            cache_key = f"calc:{sid}:{expr}"
            if not hasattr(self, "_calc_cache"):
                self._calc_cache = {}
            if cache_key in self._calc_cache:
                logger.info("calculator 同参数复用结果: %s", expr)
                cached = self._calc_cache[cache_key]
                return {
                    "record": StepRecord(
                        step_index=step_index,
                        thought=f"复用计算结果: {expr}",
                        tool_call=ToolCall(tool_name="calculator", arguments={"expression": expr}),
                        tool_result=ToolResult(
                            tool_call_id=initial_call.id,
                            tool_name="calculator",
                            success=True,
                            output=cached,
                        ),
                        round_index=step_index,
                    ),
                    "extra_tokens": 0,
                    "retried": False,
                    "final_thought": f"复用计算结果: {expr}",
                    "tool_call": ToolCall(tool_name="calculator", arguments={"expression": expr}),
                    "tool_result": ToolResult(
                        tool_call_id=initial_call.id,
                        tool_name="calculator",
                        success=True,
                        output=cached,
                    ),
                }

        thought = initial_thought
        tool_call = initial_call
        extra_tokens = 0
        max_r = settings.tool_max_retries
        retry_count = 0
        retried = False

        while True:
            try:
                tool = self.registry.get_tool(tool_call.tool_name)
                self._validate_tool_args(tool, tool_call.arguments)

                tool_start = time.perf_counter()
                output = await tool.run(**tool_call.arguments)
                tool_duration = (time.perf_counter() - tool_start) * 1000
                logger.info("工具 %s 执行完成 (%d ms)", tool_call.tool_name, round(tool_duration))

                masked_output = self.guardrails.check_tool_output(str(output), session_id=sid)
                tool_result = ToolResult(
                    tool_call_id=tool_call.id,
                    tool_name=tool_call.tool_name,
                    success=True,
                    output=masked_output,
                    duration_ms=round(tool_duration, 2),
                )
                # calculator 同参数缓存：本轮后续相同表达式直接复用
                if tool_call.tool_name == "calculator":
                    expr = str(tool_call.arguments.get("expression", "")).strip()
                    if expr:
                        if not hasattr(self, "_calc_cache"):
                            self._calc_cache = {}
                        self._calc_cache[f"calc:{sid}:{expr}"] = masked_output
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
                retried = True
                # 失败的 thought 一并写入，LLM 能看到自己上一步的动作
                memory.add(AgentMessage(role=ChatRole.assistant, content=thought))
                memory.add(AgentMessage(
                    role=ChatRole.tool,
                    content=f"[工具 {tool_call.tool_name}] 执行失败(重试{retry_count}/{max_r}): {e}",
                    tool_name=tool_call.tool_name,
                    tool_call_id=tool_call.id,
                ))

                messages = self._build_messages(system_prompt, memory, chapters, state_note)
                tools = self._native_tools()
                tc_sink2: dict = {}
                chat_kwargs: dict = {"temperature": settings.temperature}
                chat_messages = messages
                if tools:
                    chat_kwargs["tools"] = tools
                    chat_kwargs["tool_call_sink"] = tc_sink2
                    chat_messages = self._to_native_messages(messages)
                reply = await self.llm.chat_async(chat_messages, **chat_kwargs)
                thought = reply.content
                tokens = reply.total_tokens or estimate_tokens(thought)
                self._record_llm(req_metrics, tokens)
                extra_tokens += tokens

                native_calls = (tc_sink2.get("tool_calls") if tools else None) \
                    or reply.tool_calls
                if native_calls:
                    nc = native_calls[0]
                    thought = thought or f"调用工具 {nc['name']}"
                    new_call = ToolCall(tool_name=nc["name"], arguments=nc["arguments"])
                else:
                    new_call = None
                if new_call is None:
                    logger.info("子任务 %s 工具失败后 LLM 给出结论: %s", sid, thought[:80])
                    memory.add(AgentMessage(role=ChatRole.assistant, content=thought))
                    return "", thought
                tool_call = new_call
                continue

                logger.warning(
                    "工具 %s 执行失败(第%d次), 交由 LLM 修正: %s", tool_call.tool_name, retry_count, e
                )
                retried = True
                # 失败的 thought 一并写入，LLM 能看到自己上一步的动作
                memory.add(AgentMessage(role=ChatRole.assistant, content=thought))
                memory.add(AgentMessage(
                    role=ChatRole.tool,
                    content=f"[工具 {tool_call.tool_name}] 执行失败(重试{retry_count}/{max_r}): {e}",
                    tool_name=tool_call.tool_name,
                    tool_call_id=tool_call.id,
                ))

                messages = self._build_messages(system_prompt, memory, chapters, state_note)
                tools = self._native_tools()
                tc_sink2: dict = {}
                chat_kwargs: dict = {"temperature": settings.temperature}
                chat_messages = messages
                if tools:
                    chat_kwargs["tools"] = tools
                    chat_kwargs["tool_call_sink"] = tc_sink2
                    chat_messages = self._to_native_messages(messages)
                reply = await self.llm.chat_async(chat_messages, **chat_kwargs)
                thought = reply.content
                tokens = reply.total_tokens or estimate_tokens(thought)
                self._record_llm(req_metrics, tokens)
                extra_tokens += tokens

                native_calls = (tc_sink2.get("tool_calls") if tools else None) \
                    or reply.tool_calls
                if native_calls:
                    nc = native_calls[0]
                    thought = thought or f"调用工具 {nc['name']}"
                    new_call: ToolCall | None = ToolCall(
                        tool_name=nc["name"], arguments=nc["arguments"])
                else:
                    new_call = None
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
            "retried": retried,
        }

    # ── 终态统一出口 ──────────────────────────────────

    def _finalize_answer(
        self, raw_answer: str, *, step_index: int, parsed: list[ToolCall],
        memory: ShortTermMemory, req_metrics: MetricsCollector, steps: list[StepRecord],
        total_tokens: int, start_time: float, sid: str, wm: WorkingMemory,
        tool_name: str | None = None,
    ) -> tuple[AgentResult, StepRecord, str, str]:
        """统一终态：脱敏 → 入记忆 → 记追踪 → 追加 step → 算耗时 → 产出 AgentResult。

        返回 (result, record, filtered, raw)，调用方据此决定是否推送 answer_replace
        事件（覆盖流式阶段已展示的、可能含敏感信息的原始文本）。
        """
        filtered = self.guardrails.check_output(raw_answer, session_id=sid)
        if not filtered.strip():
            filtered = "（本次未产生有效回复，请换个问法或稍后再试。）"
        # 先脱敏再入记忆/历史，防止敏感信息经上下文回流
        memory.add(AgentMessage(role=ChatRole.assistant, content=filtered))
        self.tracer.record_step(step_index, filtered, None, None, session_id=sid)

        tool_call = parsed[0] if parsed else None
        tool_result: ToolResult | None = None
        if tool_call is not None:
            tool_result = ToolResult(
                tool_call_id=tool_call.id,
                tool_name=tool_name or tool_call.tool_name,
                success=True,
                output=filtered,
            )
        record = StepRecord(
            step_index=step_index,
            thought=filtered,
            round_index=step_index,
            tool_call=tool_call,
            tool_result=tool_result,
        )
        steps.append(record)

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
        return result, record, filtered, raw_answer

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

    @staticmethod
    def _build_messages(
        system_prompt: str,
        memory: ShortTermMemory,
        chapters: list[str] | None = None,
        state_note: str | None = None,
    ) -> list[AgentMessage]:
        """消息数组 = [system][章节1..k(冻结)][历史…][状态尾注][最新输入]

        - 章节为独立 system 消息：压缩事件只 append 新章，旧章节与系统提示词
          的缓存永不失效；每章含该周期压缩摘要 + 该周期结束时的 WM 快照
        - 历史 strictly append-only
        - 状态尾注仅承载当期工作记忆（周期开始时已清零）+ 跨会话召回
        """
        messages: list[AgentMessage] = [
            AgentMessage(role=ChatRole.system, content=system_prompt)
        ]
        chapter_list = chapters or []
        total_ch = len(chapter_list)
        for idx, ch in enumerate(chapter_list):
            messages.append(AgentMessage(
                role=ChatRole.system,
                content=(
                    f"## 历史记忆章节（第{idx + 1}/{total_ch}段，较早→较近的过往对话归档，"
                    "事实仍有效但已结束）\n" + ch
                ),
            ))
        ctx = memory.get_context()
        head, tail = ctx[:-1], ctx[-1:]
        messages.extend(head)
        if state_note:
            messages.append(AgentMessage(
                role=ChatRole.system,
                content="## 当前任务状态（每轮更新，以此为准）\n" + state_note,
            ))
        messages.extend(tail)
        return messages

    @staticmethod
    def _generate_id() -> str:
        import hashlib
        import uuid

        return hashlib.md5(uuid.uuid4().bytes).hexdigest()[:12]

    @staticmethod
    def _validate_tool_args(tool: BaseTool, args: dict[str, Any]) -> None:
        required = tool.spec.parameters.get("required", [])
        for key in required:
            value = args.get(key)
            if key not in args or (isinstance(value, str) and not value.strip()):
                raise ToolError(f"工具 '{tool.spec.name}' 缺少必填参数: {key}")
