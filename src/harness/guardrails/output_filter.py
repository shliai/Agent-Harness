from __future__ import annotations

import re
from typing import Any

from harness.guardrails.base import BaseGuardrail

# 敏感信息模式（模块级，供输出过滤与审计脱敏复用）
SENSITIVE_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)"), "***"),   # 身份证 18 位（末位可 X/x）
    (re.compile(r"(?<!\d)\d{15}(?!\d)"), "***"),          # 身份证 15 位
    (re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)"), "***"),     # 手机号
    (re.compile(r"(?<!\d)\d{16,19}(?!\d)"), "***"),       # 银行卡号
    (re.compile(r"sk-[a-zA-Z0-9]{20,}"), "***"),          # API Key
]


def mask_sensitive(text: str) -> str:
    """将文本中的敏感信息替换为 ***（不追加提示语，供审计等场景复用）"""
    for pattern, replacement in SENSITIVE_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


class OutputFilter(BaseGuardrail):
    """输出过滤：屏蔽敏感信息（身份证/手机号/银行卡/API Key）"""

    name = "output_filter"

    def check(self, context: dict[str, Any]) -> str | None:
        if context.get("type") not in ("output", "tool_output"):
            return None

        text: str = context.get("content", "")
        original = text
        text = mask_sensitive(text)

        if text != original:
            text += "\n\n（注意：以上回复已自动屏蔽敏感信息）"
            return text

        return None
