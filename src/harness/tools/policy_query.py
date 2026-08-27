from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any

from harness.tools.base import BaseTool, ToolSpec

logger = logging.getLogger("harness.tools.policy")

POLICY_PATH = Path("data/policies.json")

# 中文高频停用词：不参与政策匹配打分，避免「的/了/什么」等无信息量词
# 与任意政策正文共现导致误命中（返回无关条款=变相编造）
_POLICY_STOPWORDS = {
    "的", "了", "吗", "呢", "吧", "啊", "哦", "嗯", "是", "在", "有", "和",
    "与", "或", "及", "等", "这", "那", "什么", "怎么", "为什么", "一个",
    "可以", "能不能", "是否", "请问", "你好", "您", "我", "你", "他", "她",
}


class PolicyQueryTool(BaseTool):
    """结构化政策库检索：退换货/保修/价保/发票/配送等官方口径

    政策类问题必须查这里回答，禁止 LLM 凭记忆编造条款。
    小语料（~10 条）用 BM25 关键字打分即可，无需向量库。
    """

    spec = ToolSpec(
        name="policy_query",
        description="查询平台官方政策：七天无理由退货、质量问题退换、退款时效、保修、价保、发票、配送时效、换货流程、物流赔付、账户安全等。凡涉及政策条款、'能否退/能否换/是否支持'等可行性问题必须先调用本工具。**仅提供政策条文；具体订单的售后进度请用 after_sale_query，发起申请请用 after_sale_apply。**",
        parameters={
            "type": "object",
            "properties": {
                "topic": {
                    "type": "string",
                    "description": "政策主题或用户问题，如'激活过的耳机能不能退'",
                }
            },
            "required": ["topic"],
        },
    )

    def __init__(self) -> None:
        self._policies: list[dict] = []
        try:
            if POLICY_PATH.exists():
                self._policies = json.loads(POLICY_PATH.read_text(encoding="utf-8"))
            logger.info("PolicyQueryTool 初始化完成 (%d 条政策)", len(self._policies))
        except Exception as e:
            logger.warning("政策库加载失败，降级为空库: %s", e)

    async def run(self, **kwargs: Any) -> str:
        topic = str(kwargs.get("topic", "")).strip()
        if not topic:
            return "请描述您想咨询的政策问题"

        if not self._policies:
            return "政策库暂不可用，相关问题将转人工核实，请稍由人工客服为您确认。"

        from harness.tools.context import current_session_id
        from harness.tools.knowledge_retrieval import BM25, tokenize

        corpus = [f"{p['title']} {' '.join(p.get('tags', []))} {p['content']}" for p in self._policies]
        bm25 = BM25([tokenize(doc) for doc in corpus])
        q_tokens = [t for t in tokenize(topic) if t not in _POLICY_STOPWORDS]
        scored = sorted(
            range(len(self._policies)), key=lambda i: -bm25.score(q_tokens, i)
        )
        hits = [i for i in scored[:2] if bm25.score(q_tokens, i) > 0]

        if not hits:
            # 未命中 → 明确告知并引导转人工，绝不编造
            return (
                f"未在官方政策库中找到与「{topic[:40]}」直接对应的条款。"
                "此类情况需人工核实，请使用 transfer_human 工具为用户转接，"
                "不要向用户承诺任何未经核实的处理方式。"
            )

        lines = [f"根据平台官方政策（共命中 {len(hits)} 条）:"]
        for i in hits:
            p = self._policies[i]
            lines.append(f"\n【{p['title']}】({p['id']})\n{p['content']}")
        lines.append("\n（请严格依据以上条款回答，不得扩展或编造）")
        return "\n".join(lines)


class TransferHumanTool(BaseTool):
    """转人工：创建工单并返回确认话术"""

    spec = ToolSpec(
        name="transfer_human",
        description=(
            "将当前会话转接人工客服并创建工单。触发条件：用户明确要求人工；"
            "同一问题尝试 2 次仍无法解决；涉及投诉、赔偿、越权诉求等你无权处理的事项"
        ),
        parameters={
            "type": "object",
            "properties": {
                "reason": {
                    "type": "string",
                    "description": "转人工原因摘要，如'用户申请超7天退货'",
                },
                "order_id": {
                    "type": "string",
                    "description": "相关订单号（可选）",
                },
            },
            "required": ["reason"],
        },
    )

    async def run(self, **kwargs: Any) -> str:
        from harness.config import settings
        from harness.tools.context import current_session_id, current_user_id

        reason = str(kwargs.get("reason", "")).strip() or "用户要求人工服务"
        order_id = str(kwargs.get("order_id", "")).strip()

        ticket_id = f"TK{int(time.time() * 1000):013X}"
        record = {
            "ticket_id": ticket_id,
            "session_id": current_session_id.get(),
            "user_id": current_user_id.get(),
            "reason": reason[:200],
            "order_id": order_id or None,
            "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "status": "pending",
        }

        saved = True
        try:
            ticket_dir = Path(settings.data_dir) / "tickets"
            ticket_dir.mkdir(parents=True, exist_ok=True)
            path = ticket_dir / f"tickets_{time.strftime('%Y%m%d')}.jsonl"
            with path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
        except Exception as e:
            saved = False
            logger.warning("工单写入失败(仍会返回转接话术): %s", e)

        logger.info("转人工工单: %s | %s | %s", ticket_id, record["user_id"], reason)
        suffix = "" if saved else "（工单系统繁忙，记录可能延迟）"
        order_note = f"，已关联订单 {order_id}" if order_id else ""
        return (
            f"已完成人工转接{order_note}。工单号 {ticket_id}，人工客服将在工作时间内优先处理您的会话，"
            f"请保持联系方式畅通。{suffix}"
        )
