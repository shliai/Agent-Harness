from __future__ import annotations

import re

from harness.memory.working_memory import WorkingMemory

FINALIZE_TOOL = "respond"
PLAN_TOOL = "plan"

# 寒暄/无信息量输入：跳过轮末学习与长期记忆写入
_TRIVIAL_INPUT_RE = re.compile(
    r"^(你好|您好|您好呀|hello|hi|hey|在吗|在么|谢谢|多谢|感谢|辛苦了|"
    r"好的|好滴|嗯+|哦+|ok|okay|收到|明白了|知道了|再见|拜拜)[!！？?。～~，,.\s]*$",
    re.IGNORECASE,
)


# 商品意图强制检索（防幻觉护栏）：模型本轮未主动调工具时，命中该正则
# 即强制回滚直接回答、改为调用 knowledge_retrieval——宁可多查一次，不可凭记忆编造
# 仅用 品类词/品牌词/意图词/价格预算 触发；不含评价词（好不好/怎么样），避免闲聊误触发
_PRODUCT_INTENT_RE = re.compile(
    r"(?:手机|笔记本|电脑|平板|耳机|手表|手环|相机|音箱|电视|冰箱|洗衣机|空调|"
    r"充电器|键盘|鼠标|显示器|智能家居|穿戴设备|机械键盘|"
    r"苹果|华为|小米|荣耀|oppo|vivo|三星|索尼|联想|戴尔|惠普|华硕|大疆|"
    r"推荐|哪款|型号|参数|配置|比价|对比|性价比|值得买|"
    r"价格|多少钱|价位|预算|库存|现货|有货|断货|缺货|补货)",
    re.IGNORECASE,
)

# ── 指代追问式只读查询的前置拦截 ─────────────────────────
# 输入含指代词/进度问法且工作记忆持有对应实体时，跳过模型决策直接强制
# 调用对应只读工具——根治「这个订单到哪了」被当闲聊回答的漂移。
_ANAPHORA_RE = re.compile(r"(这个|那个|这单|那单|该订单|最近那笔|上一笔|刚才那|它)")
_AFTERSALE_PROGRESS_RE = re.compile(r"(退款|退货|售后|换货)[^。？?]{0,8}(进度|到账|处理|结果|怎么样)|(退款进度|售后进度)")
_AFTERSALE_INTENT_RE = re.compile(r"(退货|换货|退款|售后)")
_LOGISTICS_HINT_RE = re.compile(r"(物流|快递|包裹|运输|配送)")
_ORDER_STATUS_RE = re.compile(r"(状态|到哪|哪里了|发货了吗|什么时候(到|发货|送达)|签收了吗|收到了吗|进度)")
_NEW_ORDER_ID_RE = re.compile(r"(?<!\d)20\d{9,13}(?!\d)")
_NEW_TRACKING_RE = re.compile(r"(?i)(?<![A-Z0-9])(SF|YT|ZTO|STO|JD|EMS)\d{9,12}(?!\d)")

# 计算类：显式算式或明确要求计算 → 强制 calculator（杜绝心算偏差/漏调）
_EXPR_RE = re.compile(
    r"[\d.()]+\s*[\+\-\*×÷/%(]\s*[\d.()]+(?:\s*[\+\-\*×÷/%(]\s*[\d.()]+)*"
)
_CALC_RE = re.compile(r"(?:" + _EXPR_RE.pattern + r")|算一下|计算|等于多少|算出来")
# 投诉 / 赔偿 / 纠纷 / 转人工 → 强制 transfer_human
_COMPLAINT_RE = re.compile(r"投诉|赔偿|纠纷|维权|转人工|叫人|人工客服|找人工|处理不了|解决不了")
# 政策可行性（能否/可以 + 退换/保修/价保/发票等）→ 强制 policy_query；
# 须排在售后意图分支之前，否则「能不能退」会被误判为找单/售后流程
_POLICY_FEAS_RE = re.compile(
    r"(?:能|可以|行|支持|是否|允许|怎么).{0,6}(?:退|换|保修|价保|发票|退换|政策)"
    r"|(?:退|换|保修|价保|发票|退换).{0,6}(?:吗|么|不|行|可以|能|支持)"
)
# 查本人订单列表（不记得单号 / 我买过什么）→ 强制 order_list
_MY_ORDERS_RE = re.compile(
    r"我买过|我下过|我的订单|买过什么|买过哪些|买过啥|下过哪些|不记得订单号|看看最近买|最近买|我买的东西|订单列表"
)

# 组合 / 套装 / 多套配置类意图：不应触发「单条宽泛强制检索」，
# 交由模型按协议分解为每品类一次结构化检索
_BUNDLE_INTENT_RE = re.compile(
    r"全家桶|套装|组合|搭配|多套|几套|各来|分别买|每.{0,3}一套|配\s*\d+\s*套|一套.*一套|凑\s*\d+\s*件",
    re.IGNORECASE,
)


def _plan_forced_readonly(user_input: str, wm: WorkingMemory) -> tuple[str, dict] | None:
    """按优先级返回应前置强制的只读工具调用；无需强制时返回 None。

    只覆盖无副作用的查询类工具；写操作（after_sale_apply 等）永远交由
    模型按「指代追问协议」处理，避免盲发变更请求。
    实体取值优先用本轮输入里的显式单号，其次回退工作记忆最近一条。
    """
    text = user_input.strip()

    # 计算类：显式算式 / 明确要求计算 → 强制 calculator（杜绝心算偏差、漏调）
    if _CALC_RE.search(text):
        em = _EXPR_RE.search(text)
        return ("calculator", {"expression": em.group(0) if em else text})

    # 投诉 / 赔偿 / 纠纷 / 转人工 → 强制 transfer_human（reason 必填，带原话便于追溯）
    if _COMPLAINT_RE.search(text):
        return ("transfer_human", {"reason": text})

    # 售后进度（after_sale_query 无必填参数，最安全）—排在政策可行性之前，
    # 避免「退款进度」被政策分支误吞
    if _AFTERSALE_PROGRESS_RE.search(text):
        return ("after_sale_query", {})

    # 政策可行性（能否/可以退换、保修、价保、发票等）→ 强制 policy_query；
    # 必须早于售后意图分支，否则「能不能退」会被当成找单/售后流程
    if _POLICY_FEAS_RE.search(text):
        return ("policy_query", {})

    # 显式订单号 → 直接 order_query（优先级高于"我的订单"泛指，避免有单号还拉列表）
    if _NEW_ORDER_ID_RE.search(text):
        return ("order_query", {"order_id": _NEW_ORDER_ID_RE.search(text).group(0)})

    # 查本人订单列表（不记得单号 / 我买过什么，且无显式单号）→ 强制 order_list
    if _MY_ORDERS_RE.search(text):
        return ("order_list", {})

    # 售后诉求但缺显式单号 → 先强制只读找单/确认（帮用户锁定订单），
    # 提交类写操作仍由模型在拿到确认信息后按协议执行
    if _AFTERSALE_INTENT_RE.search(text) and not _NEW_ORDER_ID_RE.search(text):
        if wm.order_ids:
            return ("order_query", {"order_id": wm.order_ids[-1]})
        return ("order_list", {})

    # 物流轨迹：本轮输入的运单号优先，否则回退记忆中最近的
    if _LOGISTICS_HINT_RE.search(text):
        m = _NEW_TRACKING_RE.search(text)
        if m:
            return ("logistics_query", {"logistics_no": m.group(0)})
        if wm.tracking_nos:
            return ("logistics_query", {"logistics_no": wm.tracking_nos[-1]})

    # 订单状态：指代词/进度问法 + 记忆中有订单
    if (_ORDER_STATUS_RE.search(text) or _ANAPHORA_RE.search(text)) and wm.order_ids:
        return ("order_query", {"order_id": wm.order_ids[-1]})

    return None


# 澄清式反问检测：记录等待补充的信息项，下一轮注入任务状态防止重复追问
_CLARIFY_RE = re.compile(
    r"(?:请|麻烦|需要您)?(?:提供|告知|告诉我)[^，。？！]{0,12}?(订单号|物流单号|快递单号|订单编号|型号|问题)?"
)


def _build_forced_query(user_input: str, wm: WorkingMemory) -> str:
    """强制检索的 query：用户原话 + 工作记忆中的预算约束（若有）"""
    query = user_input.strip()
    if wm.budget_amount is not None:
        query += f" 预算上限 {int(wm.budget_amount)} 元以内"
        if wm.budget_category:
            query += f"（{wm.budget_category}）"
    return query

def estimate_tokens(text: str) -> int:
    """粗略估算：CJK ≈0.7 token/字，ASCII ≈4 chars/token（对齐主流中英混合分词器）"""
    if not text:
        return 0
    cjk = sum(1 for ch in text if ord(ch) > 0x2E7F)
    ascii_len = len(text) - cjk
    est = int(cjk * 0.7) + (ascii_len + 3) // 4
    return max(est, 1)
