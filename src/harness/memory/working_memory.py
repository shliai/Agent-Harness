from __future__ import annotations

import logging
import re
from typing import Any

from pydantic import BaseModel, Field

logger = logging.getLogger("harness.memory.working_memory")

# 订单号：与 OrderQueryTool 白名单对齐，11-15 位（种子/生产均为 2026 起的 13 位编号）
_ORDER_ID_RE = re.compile(r"(?<!\d)20\d{9,13}(?!\d)")
_TRACKING_RE = re.compile(
    r"(?i)(?<![A-Z0-9])(SF|YT|ZTO|STO|JD|EMS)\d{9,12}(?!\d)"
)
_MAX_LIST = 10
# 金额信号：数字 + 元/块/¥/rmb（用于抽取预筛，与 _update_budget 的槽位逻辑互补）
_AMOUNT_RE = re.compile(r"\d+(?:\.\d+)?\s*(?:元|块|¥|rmb)", re.IGNORECASE)

# ── 用户偏好/约束/纠正：确定性信号（长期学习机制的唯一定点抽取源） ──
# 与长期记忆共用，避免二次正则/LLM 抽取；工作记忆每轮抽取一次，长期记忆轮末直接读取。
_PREF_MARK_RE = re.compile(r"(喜欢|偏好|倾向于|常买|一般买|习惯用|只用|只买|要\s*[^，,]{0,8}牌)")
_BRAND_RE = re.compile(
    r"(索尼|华为|苹果|小米|三星|海尔|美的|格力|联想|戴森|飞利浦|松下|佳能|尼康|比亚迪|宝马|奔驰|奥迪|耐克|阿迪达斯)"
)
_CONSTRAINT_RE = re.compile(r"对(.{1,8}?)(过敏|忌口|禁用|不耐受)")
_CONSTRAINT2_RE = re.compile(r"(忌|避开|不要|避免)\s*(\S{1,6})(材质|面料|成分|牌子)")
_CORRECT_RE = re.compile(r"(不是|不对|搞错|弄错)([^。；;]{0,12}?)(?:是|要|应该是)\s*([^，,。；;]+)")
_CORRECT2_RE = re.compile(r"我要的是\s*([^，,。；;]+)")


def _extract_category_static(text: str) -> str | None:
    """品类识别复用 domain 层的过滤抽取（确定性，不依赖 tools 层）"""
    from harness.domain.query_parsing import extract_category

    return extract_category(text)


def extract_user_prefs(text: str) -> list[tuple[str, str]]:
    """显式偏好标记 + 品牌/品类识别（确定性），返回 [(key, value)]"""
    out: list[tuple[str, str]] = []
    if _PREF_MARK_RE.search(text):
        cat = _extract_category_static(text)
        if cat:
            out.append(("category", f"品类={cat}"))
        m = _BRAND_RE.search(text)
        if m:
            out.append(("brand", f"品牌={m.group(1)}"))
    return out


def extract_user_constraints(text: str) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    m = _CONSTRAINT_RE.search(text)
    if m:
        out.append(("allergy", f"对{m.group(1)}{m.group(2)}"))
    m = _CONSTRAINT2_RE.search(text)
    if m:
        out.append(("material", f"{m.group(1)}{m.group(2)}{m.group(3)}"))
    return out


def extract_user_corrections(text: str) -> tuple[str, str] | None:
    def _classify(phrase: str) -> tuple[str, str]:
        brand = _BRAND_RE.search(phrase)
        if brand:
            return ("brand", f"品牌={brand.group(1)}")
        cat = _extract_category_static(phrase)
        if cat:
            return ("category", f"品类={cat}")
        return ("preference", phrase)

    m = _CORRECT_RE.search(text)
    if m:
        phrase = m.group(m.lastindex).strip(" ，,。；;")
        return _classify(phrase)
    m = _CORRECT2_RE.search(text)
    if m:
        phrase = m.group(1).strip(" ，,。；;")
        return _classify(phrase)
    return None


class WorkingMemory(BaseModel):
    """跨轮工作记忆：结构化任务状态槽位 + 实体关系事实（当期周期）

    与短期记忆（原始消息窗口）互补：
    - 短期记忆给 LLM 看「最近说了什么」，超窗即丢
    - 工作记忆存「任务里最关键的少量事实」，全程持久、注入状态尾注
      （预算约束 / 订单号 / 物流单号 / 近期话题 / 关键事实），不依赖 LLM 的注意力

    槽位由确定性规则从用户输入中抽取，零额外延迟；用户偏好/约束/纠正
    同样由规则抽取，作为长期「学习机制」的唯一定点数据源（无 LLM 自由抽取）。

    生命周期：压缩事件时整块烘焙进冻结章节后 reset_for_new_cycle() 清零，
    新周期从空白开始重新积累——章节负责历史，工作记忆只管当下。
    """

    budget_amount: float | None = None
    budget_category: str | None = None
    budget_turn: int | None = None  # 预算设定的轮次（用于 prompt 标注）
    order_ids: list[str] = Field(default_factory=list)
    tracking_nos: list[str] = Field(default_factory=list)
    # 用户偏好/约束/纠正：由 update_from_input 的确定性信号抽取（长期学习机制数据源）
    user_prefs: list[str] = Field(default_factory=list)
    user_constraints: list[str] = Field(default_factory=list)
    user_corrections: list[str] = Field(default_factory=list)
    awaiting_slot: str | None = None  # 上一轮向用户发起的澄清（等待补充的信息）
    pending_plan: dict | None = None  # 模型已提案(plan)并等待用户确认/补充参数的执行方案
    # 轮末折叠式滚动摘要 S_t（cheap_llm 生成）：记录"对话进展到哪了"。
    # 只在本会话/本周期内有效：随 WM 烘焙前作废（不进章节快照），
    # reset_for_new_cycle 后由新周期重新积累
    rolling_summary: str = ""
    tokens_used: int = 0  # 会话累计 token 消耗（预算控制，跨周期保留）
    budget_warned: bool = False  # 已触发超预算告警（持久化，避免重启后重复告警）
    budget_alerted: bool = False  # 已触达告警线（持久化，避免重启后重复告警）
    updated_turn: int = 0

    # ── 序列化 ─────────────────────────────────────────

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> WorkingMemory:
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
            and not self.user_prefs
            and not self.user_constraints
            and not self.user_corrections
            and not self.rolling_summary
            and not self.awaiting_slot
        )

    def reset_for_new_cycle(self) -> None:
        """压缩事件后清空知识槽位开启新周期；仅保留会话级计数器与预算告警标志"""
        kept_tokens = self.tokens_used
        kept_turn = self.updated_turn
        kept_warned, kept_alerted = self.budget_warned, self.budget_alerted
        fresh = WorkingMemory(
            tokens_used=kept_tokens, updated_turn=kept_turn,
            budget_warned=kept_warned, budget_alerted=kept_alerted,
        )
        self.__dict__.update(fresh.__dict__)
        logger.info("工作记忆已随章节烘焙清零，进入新周期")

    # ── 更新（规则抽取，确定性） ────────────────────────

    @staticmethod
    def has_hard_entity_signal(text: str) -> bool:
        """文本中是否存在订单号/物流号/金额/预算等确定性硬信号。

        供旁路小模型调用的门控预筛：命中说明该轮含值得处理的硬信息，
        未命中则可用「输入长度 / 意图偏好关键词」兜底，减少不必要的模型调用。
        （历史用途是轮末 LLM 事实抽取预筛，该机制已由确定性学习信号替代）"""
        return bool(
            _ORDER_ID_RE.search(text)
            or _TRACKING_RE.search(text)
            or _AMOUNT_RE.search(text)
            or "预算" in text
        )

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
        self._capture_user_signals(text)

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

        category = _extract_category_static(text)
        if category:
            self.budget_category = category
        if changed:
            self.budget_turn = turn
            logger.info("工作记忆更新预算: %s 元 (%s)", amount, self.budget_category)

    def set_awaiting(self, slot_desc: str) -> None:
        """记录本轮向用户发起的澄清（由循环在最终回答含反问时调用）"""
        self.awaiting_slot = slot_desc[:60]

    def set_pending_plan(self, actions: list[str], message: str) -> None:
        """记录模型已提出的工具执行方案，等待用户确认/补充参数后本轮执行。"""
        self.pending_plan = {"actions": list(actions or []), "message": message}

    def clear_pending_plan(self) -> None:
        self.pending_plan = None

    # ── 用户信号抽取（确定性，长期学习机制数据源） ──────

    @staticmethod
    def _add_unique(lst: list[str], item: str, cap: int = 20) -> None:
        if item not in lst:
            lst.append(item)
            if len(lst) > cap:
                del lst[: len(lst) - cap]

    def _capture_user_signals(self, text: str) -> None:
        """从输入抽取用户偏好/约束/纠正（确定性），写入对应槽位。
        纠正会覆盖同 key 的既有偏好，保持会话内一致。

        约束以「key=value」编码存储（如 allergy=对镍过敏），类型在抽取点
        即已确定，learning_signals 导出时无需再靠前缀猜测。"""
        for _key, val in extract_user_prefs(text):
            self._add_unique(self.user_prefs, val)
        for key, val in extract_user_constraints(text):
            self._add_unique(self.user_constraints, f"{key}={val}")
        corr = extract_user_corrections(text)
        if corr:
            val = corr[1]
            self._add_unique(self.user_corrections, val)
            # 覆盖同 key 偏好（如「品牌=华为」被「品牌=苹果」纠正）
            prefix = val.split("=", 1)[0] + "="
            self.user_prefs = [p for p in self.user_prefs if not p.startswith(prefix)]

    def learning_signals(self) -> list[tuple[str, str, str]]:
        """导出长期学习机制所需的（type, key, value）信号。
        长期记忆轮末直接读取工作记忆，避免二次抽取。"""
        out: list[tuple[str, str, str]] = []
        if self.budget_amount is not None:
            val = f"预算上限={int(self.budget_amount)}元"
            if self.budget_category:
                val += f"（品类：{self.budget_category}）"
            out.append(("preference", "budget", val))
        for p in self.user_prefs:
            key = p.split("=", 1)[0]
            out.append(("preference", key, p))
        for c in self.user_constraints:
            key, sep, val = c.partition("=")
            if not sep:  # 兼容历史数据中的裸值
                key, val = "constraint", c
            out.append(("constraint", key, val))
        for cor in self.user_corrections:
            key = cor.split("=", 1)[0]
            out.append(("correction", key, cor))
        return out

    # ── Prompt 注入 ────────────────────────────────────

    def prompt_block(self, *, for_archive: bool = False) -> str:
        """渲染为状态块；空状态返回空串。

        for_archive=False（默认）：活侧尾注，渲染全部当期状态；
        for_archive=True：压缩烘焙用——只渲染跨周期仍生效的硬实体槽位
        （预算/订单号/物流号），进行时态信息（等待澄清/滚动摘要）不进档案：
        摘要的叙事职责由章节自带的 LLM 周期摘要承担，重复存两份会互相漂移。"""
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

        if for_archive:
            return "\n".join(lines)

        if self.rolling_summary:
            lines.append(f"- 对话进展（摘要）：{self.rolling_summary}")
        if self.awaiting_slot:
            lines.append(f"- 等待用户提供：{self.awaiting_slot}（若用户本轮已给出则直接使用，勿再追问）")
        if self.pending_plan:
            actions = self.pending_plan.get("actions") or []
            msg = self.pending_plan.get("message") or ""
            act_line = "、".join(actions) if actions else "（待用户确认/补充参数）"
            lines.append(
                f"- 你已向用户提出执行方案：{act_line}。用户本轮已回复，请据此执行对应工具并以"
                f"正确参数填入，不要把方案本身当作最终答案重复念出。"
            )
            if msg:
                lines.append(f"  （上次提问：{msg}）")

        return "\n".join(lines)
