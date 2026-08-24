from __future__ import annotations

import logging
import re
from typing import Any

from pydantic import BaseModel, Field

logger = logging.getLogger("harness.memory.working_memory")

_ORDER_ID_RE = re.compile(r"(?<!\d)20\d{9}(?!\d)")
_TRACKING_RE = re.compile(
    r"(?i)(?<![A-Z0-9])(SF|YT|ZTO|STO|JD|EMS)\d{9,12}(?!\d)"
)
_MAX_LIST = 10
_MAX_TOPICS = 5


class WorkingMemory(BaseModel):
    """跨轮工作记忆：结构化任务状态槽位

    与短期记忆（原始消息窗口）互补：
    - 短期记忆给 LLM 看「最近说了什么」，超窗即丢
    - 工作记忆存「任务里最关键的少量事实」，全程持久、注入 system prompt
      （预算约束 / 订单号 / 物流单号 / 近期话题），不依赖 LLM 的注意力

    槽位由确定性规则从用户输入中抽取，无 LLM 参与，零额外延迟。
    """

    budget_amount: float | None = None
    budget_category: str | None = None
    budget_turn: int | None = None  # 预算设定的轮次（用于 prompt 标注）
    order_ids: list[str] = Field(default_factory=list)
    tracking_nos: list[str] = Field(default_factory=list)
    recent_topics: list[str] = Field(default_factory=list)
    awaiting_slot: str | None = None  # 上一轮向用户发起的澄清（等待补充的信息）
    tokens_used: int = 0  # 会话累计 token 消耗（预算控制）
    updated_turn: int = 0

    # ── 序列化 ─────────────────────────────────────────

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "WorkingMemory":
        if not data:
            return cls()
        allowed = set(cls.model_fields.keys())
        try:
            return cls(**{k: v for k, v in data.items() if k in allowed})
        except Exception as e:
            logger.warning("工作记忆状态损坏，已重置: %s", e)
            return cls()

    def to_dict(self) -> dict[str, Any]:
        return self.model_dump()

    def is_empty(self) -> bool:
        return (
            self.budget_amount is None
            and not self.order_ids
            and not self.tracking_nos
            and not self.recent_topics
        )

    # ── 更新（规则抽取，确定性） ────────────────────────

    def update_from_input(self, text: str, turn: int) -> None:
        """从用户输入抽取关键状态。每轮调用一次。"""
        self.updated_turn = turn
        # 用户已回复 → 上轮的澄清等待视为已满足，由 LLM 从最新消息中取用
        self.awaiting_slot = None

        for oid in _ORDER_ID_RE.findall(text):
            if oid not in self.order_ids:
                self.order_ids.append(oid)
                self.order_ids = self.order_ids[-_MAX_LIST:]

        for m in _TRACKING_RE.finditer(text):
            no = m.group(0).upper()
            if no not in self.tracking_nos:
                self.tracking_nos.append(no)
                self.tracking_nos = self.tracking_nos[-_MAX_LIST:]

        self._update_budget(text, turn)

        topic = re.sub(r"\s+", " ", text.strip())[:40]
        if topic and topic not in self.recent_topics:
            self.recent_topics.append(topic)
            self.recent_topics = self.recent_topics[-_MAX_TOPICS:]

    def _update_budget(self, text: str, turn: int) -> None:
        """预算槽位：仅「明确预算表达」写入长期约束（预算3000 / 3999的手机 / 3k）；
        单纯上下限表达（"5000以下"）视为临时筛选，不覆盖既有预算"""
        # 上限表达（以内/以下），且没有"预算"字样 → 临时筛选
        cap_only = bool(re.search(r"\d+(?:\.\d+)?\s*(?:元|块)?\s*以[下内]", text))
        budget_word = "预算" in text

        amount: float | None = None

        m = re.search(r"预算[^\d]{0,4}(\d+(?:\.\d+)?)", text)
        if m:
            amount = float(m.group(1))
        else:
            m = re.search(r"(\d+(?:\.\d+)?)\s*预算", text)
            if m:
                amount = float(m.group(1))
            else:
                m = re.search(r"(\d+(?:\.\d+)?)\s*(?:元|块)?\s*的", text)
                if m:
                    amount = float(m.group(1))
                else:
                    m = re.search(r"(\d+(?:\.\d+)?)\s*[kK千]", text)
                    if m and (budget_word or not cap_only):
                        amount = float(m.group(1)) * 1000

        if amount is None or not (100 <= amount <= 99999):
            return
        if cap_only and not budget_word and self.budget_amount is not None:
            return  # 已有长期预算时，临时上限不覆盖
        if cap_only and not budget_word and "的" not in text:
            return  # 无任何预算语义，纯价格上限提问

        changed = amount != self.budget_amount
        self.budget_amount = amount

        from harness.tools.knowledge_retrieval import KnowledgeRetrievalTool

        category = KnowledgeRetrievalTool._extract_filters(text).get("category")
        if category:
            self.budget_category = category
        if changed:
            self.budget_turn = turn
            logger.info("工作记忆更新预算: %s 元 (%s)", amount, self.budget_category)

    def set_awaiting(self, slot_desc: str) -> None:
        """记录本轮向用户发起的澄清（由循环在最终回答含反问时调用）"""
        self.awaiting_slot = slot_desc[:60]

    # ── Prompt 注入 ────────────────────────────────────

    def prompt_block(self) -> str:
        """渲染为 system prompt 片段；空状态返回空串"""
        if self.is_empty():
            return ""

        lines: list[str] = ["## 任务状态（跨轮工作记忆）"]

        if self.budget_amount is not None:
            parts = [f"- 用户预算上限：{int(self.budget_amount)} 元"]
            if self.budget_category:
                parts[0] += f"（品类：{self.budget_category}）"
            if self.budget_turn is not None:
                parts[0] += f"，第 {self.budget_turn} 轮设定"
            parts[0] += "。该预算持续生效，推荐时必须合并到检索 query 中；若用户本轮明确提出新预算则以最新为准"
            lines.extend(parts)

        if self.order_ids:
            lines.append(f"- 会话中提到过的订单号：{'、'.join(self.order_ids)}")
        if self.tracking_nos:
            lines.append(f"- 会话中提到过的物流单号：{'、'.join(self.tracking_nos)}")
        if self.recent_topics:
            lines.append(f"- 近期话题：{' / '.join(self.recent_topics[-3:])}")
        if self.awaiting_slot:
            lines.append(f"- 等待用户提供：{self.awaiting_slot}（若用户本轮已给出则直接使用，勿再追问）")

        return "\n".join(lines)
