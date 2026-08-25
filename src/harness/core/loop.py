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
from harness.llm.factory import cheap_semaphore
from harness.memory.conversation_history import ConversationHistory
from harness.memory.long_term import LongTermMemory
from harness.memory.short_term import ShortTermMemory
from harness.memory.working_memory import WorkingMemory
from harness.observability.metrics import MetricsCollector
from harness.observability.tracer import Tracer
from harness.tools.base import BaseTool

logger = logging.getLogger("harness.core.loop")

# 轮末事实抽取提示词：从单轮对话提炼「实体-关系-值」结构化事实（工作记忆与长期记忆共用）
# 面向小模型设计：输出契约更死板、显式列禁止项，提升弱模型的结构遵循率
_FACT_SYSTEM_PROMPT = (
    "你是电商客服对话的信息抽取器。从本轮对话抽取「实体-关系-值」事实。\n"
    "输出要求（严格遵守）：\n"
    "- 每行一条事实，以 - 开头，格式：实体 关系 值 或 实体：关系：值\n"
    "  示例：\n"
    "  - 用户 偏好 小米品牌\n"
    "  - 订单20240601001 状态 退货申请待审核\n"
    "  - 用户 预算 3000元\n"
    "- 每条不超过 40 字；最多 6 条；只输出可靠的新信息\n"
    "- 没有值得记的新信息时，只输出一个空行，不要任何文字\n"
    "禁止：标题、序号、总结句、解释、寒暄、照抄原文整句。"
)

# 寒暄/无信息量输入：跳过轮末事实抽取与长期记忆写入
_TRIVIAL_INPUT_RE = re.compile(
    r"^(你好|您好|您好呀|hello|hi|hey|在吗|在么|谢谢|多谢|感谢|辛苦了|"
    r"好的|好滴|嗯+|哦+|ok|okay|收到|明白了|知道了|再见|拜拜)[!！？?。～~，,.\s]*$",
    re.IGNORECASE,
)

# 轮末抽取预筛：命中「硬实体信号」或「意图/偏好表达」才调小模型抽取，
# 纯寒暄/无信息轮直接跳过——小模型不每轮空转，省调用与限流配额
_INTENT_KEYWORDS = (
    "喜欢", "想要", "想", "希望", "偏好", "倾向", "考虑", "比较", "推荐",
    "麻烦", "帮忙", "帮", "要求", "介意", "不想", "不接受", "能不能",
    "可以", "需要", "了解", "看看", "咨询",
)
_EXTRACT_MIN_INPUT_LEN = 40  # 较长输入视为含实质内容（意图/想法/偏好），兜底触发抽取


def _should_extract_turn(user_input: str, answer: str) -> bool:
    """预筛本轮是否值得 LLM 抽取：硬实体信号 OR 意图/偏好表达 OR 输入较长"""
    if WorkingMemory.has_hard_entity_signal(f"{user_input}\n{answer}"):
        return True
    if len(user_input.strip()) >= _EXTRACT_MIN_INPUT_LEN:
        return True
    return any(kw in user_input for kw in _INTENT_KEYWORDS)

# 商品意图强制检索（防幻觉护栏）：模型本轮未主动调工具时，命中该正则
# 即强制回滚直接回答、改为调用 knowledge_retrieval——宁可多查一次，不可凭记忆编造
_PRODUCT_INTENT_RE = re.compile(
    r"(?:手机|笔记本|电脑|平板|耳机|手表|手环|相机|音箱|电视|冰箱|洗衣机|空调|"
    r"充电器|键盘|鼠标|显示器|智能家居|穿戴设备|"
    r"苹果|华为|小米|荣耀|oppo|vivo|三星|索尼|联想|戴尔|惠普|华硕|大疆|"
    r"推荐|哪款|型号|参数|配置|比价|对比|性价比|值得买|"
    r"价格|多少钱|价位|预算|库存|现货|好不好|怎么样)",
    re.IGNORECASE,
)


def _build_forced_query(user_input: str, wm: WorkingMemory) -> str:
    """强制检索的 query：用户原话 + 工作记忆中的预算约束（若有）"""
    query = user_input.strip()
    if wm.budget_amount is not None:
        query += f" 预算上限 {int(wm.budget_amount)} 元以内"
        if wm.budget_category:
            query += f"（{wm.budget_category}）"
    return query

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

## 商品信息强制检索（最重要）
- 凡是涉及**具体商品**的问题——推荐、比价、参数、价格、库存、品牌口碑（如"小米好不好""哪款适合我""有没有XX"）——**必须先调用 knowledge_retrieval**，禁止凭记忆或常识直接列举型号、价格、参数
- 只能引用 knowledge_retrieval 返回的商品并附商品编号；返回结果中不存在的型号、价格、参数、库存一律不得出现
- 检索无匹配时，按「空结果处理」规则应对，禁止自行编造近似型号或价格
- 品牌口碑等非商品性常识可简要说明，但不得据此编造具体在售型号、价格与参数

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
        self.long_term_memory = long_term_memory
        self._bg_tasks: set[asyncio.Task] = set()  # 后台任务持引用，防 GC
        # 轮末记忆整理的后台落盘任务（按 session 登记）：下一轮开始前 await，
        # 保证读到完整状态；同时让 [DONE] 随 result 立即送达，不再占用户感知时间
        self._pending_finalize: dict[str, asyncio.Task] = {}
        # 最近一次流式执行的 session id（execute 非流式收尾时据此等待落盘）
        self._last_stream_sid: str | None = None

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
        # 非流式调用方（CLI/测试）需拿到完整落盘状态：等待本会话的后台收尾任务
        sid = session_id or self._last_stream_sid
        pending = self._pending_finalize.get(sid) if sid else None
        if pending:
            try:
                await pending
            except Exception:
                logger.warning("等待会话落盘任务失败: %s", sid)
        return result

    async def execute_stream(
        self,
        user_input: str,
        session_id: str | None = None,
        user_id: str | None = None,
    ) -> AsyncGenerator[dict[str, Any], None]:
        """流式执行：在每个 ReAct 步骤发生时即时 yield 事件"""
        self._schedule_lt_maintenance()
        from harness.tools.context import DEFAULT_USER, current_session_id, current_user_id

        start_time = time.perf_counter()
        steps: list[StepRecord] = []
        req_metrics = MetricsCollector()  # 请求级指标：并发请求互不污染
        total_tokens = 0

        sid = session_id or self._generate_id()
        self._last_stream_sid = sid
        uid = user_id or DEFAULT_USER
        self._owner_uid = uid  # 本次会话归属（落盘与越权校验依据）
        # 请求级身份上下文：订单归属校验 / 我的订单 / 工单归属 都从这里取
        current_user_id.set(uid)
        current_session_id.set(sid)
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

            tool_descriptions = self.registry.get_tool_descriptions()
            system_prompt = SYSTEM_PROMPT_TEMPLATE.format(
                tool_descriptions=tool_descriptions or "（当前没有可用工具）"
            )

            # 上下文工程：跨轮工作记忆（预算/订单号/物流号，确定性规则维护）
            turn_no = len(memory.all_messages()) + 1
            wm.update_from_input(validated_input, turn=turn_no)

            # 状态尾注：工作记忆 + 长期记忆召回合并为一条消息，
            # 注入在对话末尾、当前输入之前——每轮变化的内容集中在尾部，
            # 前缀（系统提示词+摘要+全部历史）保持稳定，KV cache 全程命中
            state_parts: list[str] = []

            # 当期工作记忆全量渲染（压缩周期开始时已清零，体量很小）
            wm_block = wm.prompt_block()
            if wm_block:
                state_parts.append(wm_block)

            # 长期记忆：检索「其他会话」中与当前输入相关的历史对话片段
            if self.long_term_memory is not None and self.long_term_memory.enabled:
                hits = await self.long_term_memory.search(
                    validated_input, user_id=uid, exclude_session_id=sid,
                )
                if hits:
                    recall_lines = []
                    for i, h in enumerate(hits, 1):
                        # 每条截断至 160 字：召回只做线索提示，不搬运全文
                        doc = h["document"]
                        if len(doc) > 160:
                            doc = doc[:160] + "…"
                        # 时效信号：注入记录日期，帮助 LLM 判断新旧
                        ts = str(h.get("metadata", {}).get("timestamp") or "")
                        date_tag = f"({ts[:10]}) " if ts else ""
                        recall_lines.append(f"[{i}] {date_tag}{doc}")
                    state_parts.append(
                        "## 相关历史记忆\n"
                        "以下是该用户在其他会话中的相关历史片段（含日期），"
                        "若与本次问题相关可参考，无关则忽略：\n"
                        + "\n".join(recall_lines)
                    )
                    logger.info("长期记忆召回 %d 条相关历史", len(hits))

            state_note = "\n\n".join(state_parts) or None

            memory.add(AgentMessage(role=ChatRole.user, content=validated_input))

            result: AgentResult | None = None
            malformed_retries = 0

            for step_index in range(self.max_iterations):
                # 商品意图前置拦截（防幻觉护栏）：
                # 首轮模型输出前命中商品信号即强制检索并注入结果，
                # 模型直接基于检索结果作答——避免先流式输出"凭记忆"内容
                # 再被 delta_reset 回滚（前端会闪现幻觉文本后忽然消失）
                if (
                    step_index == 0
                    and "knowledge_retrieval" in self.registry.list_tools()
                    and _PRODUCT_INTENT_RE.search(validated_input)
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

                # 商品意图已在循环开头前置拦截并强制检索，此处无需重复处理
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

            # 答案先行推给用户——之后的记忆整理不占用用户感知时间
            yield {
                "type": "result",
                "result": result,
                "answer": result.answer,
                "total_duration_ms": result.total_duration_ms,
                "total_steps": len(result.steps),
                "total_tokens": total_tokens,
                "success": result.success,
            }

            # ── 记忆整理（后台任务：答案已送达，[DONE] 立即返回，UI 即时脱离推理态）──
            # 轻量 LLM 抽取「实体-关系」事实 → 合并进工作记忆 + 会话落盘 + 结构化写入长期记忆；
            # 整块放入 asyncio.create_task，生成器在此立即 return → SSE 关闭 → 前端 finally 收尾。
            # 数据一致性：任务按 session 登记到 _pending_finalize，下一轮开始前 await 它，
            # 因此紧接的下一条消息能读到完整状态；长期记忆延迟写入对单会话用法无影响。
            #
            # 垃圾防线的三种结局分流：
            #   ① LLM 异常        → 兜底确定性文档入库（数据安全）
            #   ② 抽取成功但为空   → 跳过：不入库、不动 WM（没有值得记的）
            #   ③ 抽取到事实       → WM 合并 + 事实文档入库
            owner_uid = self._owner_uid  # 快照：后台任务运行时可能已被下一请求覆盖

            async def _finalize() -> None:
                try:
                    distilled_facts: list[str] = []
                    extraction_failed = False
                    skip_memory = False
                    if (self.long_term_memory is not None
                            and self.long_term_memory.enabled):
                        if result.success is False:
                            skip_memory = True  # 失败轮：错误不是知识
                        elif _TRIVIAL_INPUT_RE.match(validated_input.strip()) \
                                or not _should_extract_turn(
                                    validated_input, result.answer
                                ):
                            skip_memory = True  # 寒暄/无信息轮：预筛未命中，不调小模型
                        else:
                            _extract_start = time.perf_counter()
                            distilled_facts, extraction_failed = \
                                await self._extract_turn_facts(
                                    validated_input, result.answer
                                )
                            _extract_ms = (time.perf_counter() - _extract_start) * 1000
                            for f in distilled_facts:
                                wm.add_fact(f)
                            if distilled_facts:
                                logger.info("轮末抽取 %d 条事实 (%d ms)", len(distilled_facts), int(_extract_ms))
                            elif extraction_failed:
                                logger.info("轮末抽取失败，走确定性兜底 (%d ms)", int(_extract_ms))

                    new_trace = {
                        "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
                        "user": validated_input[:80],
                        "steps": [s.model_dump(mode="json") for s in steps],
                    } if steps else None
                    await self._persist_session(
                        sid, memory, chapters=chapters, wm=wm,
                        prev_traces=prev_traces, new_trace=new_trace,
                        user_id=owner_uid,
                    )

                    # 长期记忆：结构化事实文档（非原文）
                    # 仅两种情况写入：抽取到事实；或 LLM 异常时兜底确定性摘要。
                    # 抽取为空 / 寒暄轮 / 失败轮 → 不写（没有值得记的信息就不制造垃圾）
                    if (self.long_term_memory is not None
                            and self.long_term_memory.enabled
                            and not skip_memory):
                        if distilled_facts:
                            doc: str | None = "\n".join(distilled_facts)
                        elif extraction_failed:
                            doc = self._deterministic_doc(
                                validated_input, result.answer, wm
                            )
                        else:
                            doc = None
                        if doc:
                            await self.long_term_memory.add(
                                user_input=validated_input,
                                assistant_answer=result.answer,
                                session_id=sid,
                                user_id=user_id,
                                document=doc,
                            )
                except Exception:
                    logger.exception("轮末记忆整理后台任务失败")

            task = asyncio.create_task(_finalize())
            self._pending_finalize[sid] = task
            task.add_done_callback(lambda t: self._pending_finalize.pop(sid, None))

        except GuardrailError as e:
            logger.warning("Guardrail 拦截: %s", e)
            err = AgentResult(answer=str(e), steps=steps, success=False, error=str(e))
            yield {"type": "error", "result": err, "message": str(e)}
        except asyncio.CancelledError:
            # 客户端断开 / 用户点停止：把已发生的部分对话与工作记忆落盘，避免整轮白聊
            logger.info("会话 %s 执行被中断，保存部分状态", sid)
            try:
                await self._persist_session(
                    sid, memory, chapters=chapters, wm=wm,
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

        # 压缩触发：单会话当前消息估算 token ≥ 窗口×比例（相对模型上下文，
        # 换模型只需调 context_window_tokens）；估算基于启发式，非精确计数
        if (
            settings.context_compress_enabled
            and msgs
            and estimate_tokens("\n".join(m.content for m in msgs))
            >= settings.context_window_tokens * settings.context_compress_ratio
        ):
            old_part, recent = memory.split_for_compression(settings.context_keep_recent)
            cycle_no = len(chapters) + 1
            chapter_summary = await self._summarize(old_part)
            if chapter_summary:
                # 组装冻结章节：本周期压缩内容 + 该周期结束时刻的 WM 快照
                snapshot = wm.prompt_block() or "（本周期无显著任务状态）"
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
            chapters=chapters, user_id=user_id or self._owner_uid,
        )

    # 压缩摘要的 system 提示词（模块级常量，测试以此识别摘要调用）
    SUMMARY_SYSTEM_PROMPT = (
        "你是电商客服 Agent 的对话档案员。你输出的摘要会作为独立上下文块注入后续对话，"
        "是客服了解该用户历史的唯一依据。只输出摘要正文，"
        "不要任何解释、寒暄或代码块标记。"
    )

    # 轮末事实抽取的 system 提示词（模块级常量的类内别名，方法体引用模块级版本）
    FACT_SYSTEM_PROMPT = _FACT_SYSTEM_PROMPT

    async def _extract_turn_facts(self, user_input: str, answer: str) -> tuple[list[str], bool]:
        """轮末轻量抽取「实体-关系」事实 ≤6 条（工作记忆与长期记忆共用）

        返回 (facts, extraction_failed)：failed=True 表示 LLM 异常（调用方走兜底），
        failed=False 且 facts 为空表示本轮确无可抽取信息（调用方跳过入库）。
        """
        try:
            # 小模型优先（旁路调用省成本）；未配置/直接构造无 cheap_llm 时回退主模型
            extractor = getattr(self, "cheap_llm", None) or self.llm
            async with cheap_semaphore:
                reply = await extractor.chat_async(
                    [
                        AgentMessage(role=ChatRole.system, content=_FACT_SYSTEM_PROMPT),
                        AgentMessage(
                            role=ChatRole.user,
                            content=f"[用户输入]\n{user_input}\n\n[助手回复]\n{answer[:800]}",
                        ),
                    ],
                    temperature=0.0,
                )
            facts: list[str] = []
            for raw in reply.content.splitlines():
                line = raw.strip().lstrip("-•*").strip()
                # 结构守卫：≥3 个词元（实体/关系/值）或带显式分隔符，避免把闲聊回复当事实
                if 4 <= len(line) <= 80 and (
                    len(line.split()) >= 3 or "-" in line or "：" in line or ":" in line
                ):
                    facts.append(line)
                if len(facts) >= 6:
                    break
            return facts, False
        except Exception as e:
            logger.warning("轮末事实抽取失败: %s", e)
            return [], True

    @staticmethod
    def _deterministic_doc(user_input: str, answer: str, wm: WorkingMemory) -> str:
        """确定性结构化文档：LLM 抽取失败时的长期记忆降级格式（绝不存原文全文）"""
        first_line = user_input.strip().splitlines()[0][:60]
        parts = [f"[诉求] {first_line}"]
        ents = []
        if wm.order_ids:
            ents.append("订单:" + ",".join(wm.order_ids[-3:]))
        if wm.tracking_nos:
            ents.append("物流:" + ",".join(wm.tracking_nos[-3:]))
        if wm.budget_amount is not None:
            cat = f"({wm.budget_category})" if wm.budget_category else ""
            ents.append(f"预算:{int(wm.budget_amount)}元{cat}")
        if ents:
            parts.append("[实体] " + "；".join(ents))
        gist = " ".join(answer.split())[:120]
        parts.append(f"[结论] {gist}")
        return "\n".join(parts)

    @staticmethod
    def _build_summary_prompt(transcript: str) -> str:
        max_chars = settings.context_summary_max_chars
        return (
            "请把以下对话记录压缩为一份阶段摘要（章节式记忆，每章独立、互不合并）。\n\n"
            "## 核心要求：提炼，不是摘录\n"
            "- 用档案语言重写：每条信息是一行客观事实陈述，禁止对话体\n"
            "- 禁止出现\"用户说\"\"客服回答\"\"然后\"等引用或叙事结构，禁止按时间顺序复述过程\n"
            "- 合并同类信息：多轮讨论的同一件事只留最终结论与当前状态\n"
            "- 唯一例外：订单号/金额等原子标识符必须与原文完全一致——"
            "这是数据完整性要求（改一个数字就是另一个订单），不等于允许照搬句子\n\n"
            "## 摘要格式\n"
            f"总长度不超过 {max_chars} 字。用以下固定字段组织，"
            "没有内容的字段整行省略，字段顺序保持不变：\n\n"
            "【当前诉求】用户此刻最想解决的一件事\n"
            "【关键标识符】订单号/售后单号/物流单号/商品编号（逐字保真）\n"
            "【金额与预算】预算上限、涉事金额、优惠规则（数字保真）\n"
            "【办理进度】每个在办事项的状态机位置，如\"订单20240601001：退货申请待商家审核\"\n"
            "【已做承诺】客服已答应的事项与数字（到账天数、运费承担等）——合规凭据，数字保真\n"
            "【未解决事项】尚未办结的问题\n"
            "【已排除选项】用户拒绝过的方案或不满意的推荐（防止重复推销）\n"
            "【用户状态】情绪信号：不满、焦虑、重复追问、转人工倾向\n\n"
            f"[待压缩的本周期对话记录]\n{transcript}"
        )

    async def _summarize(self, msgs: list[AgentMessage]) -> str | None:
        """LLM 章节压缩：把本周期旧对话按电商客服档案结构压缩成独立章节摘要"""
        try:
            transcript = "\n".join(f"{m.role.value}: {m.content}" for m in msgs)
            prompt = self._build_summary_prompt(transcript)

            reply = await self.llm.chat_async(
                [
                    AgentMessage(role=ChatRole.system, content=self.SUMMARY_SYSTEM_PROMPT),
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
        chapters: list[str] | None = None,
        state_note: str | None = None,
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
                logger.info("工具 %s 执行完成 (%d ms)", tool_call.tool_name, round(tool_duration))

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

                messages = self._build_messages(system_prompt, memory, chapters, state_note)
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

    def _schedule_lt_maintenance(self) -> None:
        """长期记忆维护：进程生命周期内只调度一次（首次对话时后台执行）"""
        if getattr(self, "_lt_maintained", False):
            return
        self._lt_maintained = True
        if self.long_term_memory is None or not self.long_term_memory.enabled:
            return

        async def _run() -> None:
            try:
                live = set(await self.conversation_history.alist_sessions())
                stats = await self.long_term_memory.maintain(live_session_ids=live)
                logger.info("长期记忆维护完成: %s", stats)
            except Exception as e:
                logger.warning("长期记忆维护失败(不影响使用): %s", e)

        task = asyncio.create_task(_run())
        self._bg_tasks.add(task)
        task.add_done_callback(self._on_bg_done)

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
        for ch in chapters or []:
            messages.append(AgentMessage(
                role=ChatRole.system,
                content="## 历史记忆章节（已归档，事实仍有效）\n" + ch,
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
