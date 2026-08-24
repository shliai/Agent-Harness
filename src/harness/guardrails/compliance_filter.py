from __future__ import annotations

import logging
import re
from typing import Any

from harness.guardrails.base import BaseGuardrail

logger = logging.getLogger("harness.guardrails.compliance")

# 绝对化承诺词：客服场景下未经政策核实不得向用户做确定性承诺。
# 命中后不做语义改写（避免曲解），统一追加免责提示并告警留痕。
_PROMISE_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"(百分百|百分之百|100%|肯定|绝对|保证|一定)(能|可以|会|给)?(退|赔|修|换|到账|通过)"),
    re.compile(r"最迟.{0,6}(天|小时)(内)?必(到|退|赔)"),
    re.compile(r"我们承诺.{0,12}(全额|全部)"),
]

_COMPLIANCE_NOTE = "\n\n（注：具体以官方政策条款与人工审核结果为准）"

# 输出内容安全：极简演示级词表。生产环境应接入内容安全服务。
_BANNED_WORDS = ("去死", "滚蛋", "脑残")


class ComplianceFilter(BaseGuardrail):
    """输出侧合规护栏

    - 绝对化承诺检测：命中即追加「以官方政策与人工审核为准」提示，并 warning 留痕
    - 内容安全：命中违禁词以 * 替换
    作用于 output / tool_output 两类上下文。
    """

    name = "compliance_filter"

    def check(self, context: dict[str, Any]) -> str | None:
        if context.get("type") not in ("output", "tool_output"):
            return None

        text: str = context.get("content", "")
        original = text

        for w in _BANNED_WORDS:
            if w in text:
                text = text.replace(w, "*" * len(w))
                logger.warning("输出含违禁词已替换: %s", w)

        hit = next((p.pattern for p in _PROMISE_PATTERNS if p.search(text)), None)
        if hit:
            logger.warning("检测到绝对化承诺表述 (pattern=%s)，已追加合规提示", hit)
            if not text.endswith(_COMPLIANCE_NOTE):
                text += _COMPLIANCE_NOTE

        return text if text != original else None
