from __future__ import annotations

import re
from typing import Any

from harness.guardrails.base import BaseGuardrail


class OutputFilter(BaseGuardrail):
    """输出过滤：屏蔽敏感信息"""

    name = "output_filter"

    def __init__(self) -> None:
        self.sensitive_patterns: list[tuple[re.Pattern[str], str]] = [
            (re.compile(r"(?<!\d)\d{18}(?!\d)"),  "***"),       # 身份证号
            (re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)"), "***"),  # 手机号
            (re.compile(r"(?<!\d)\d{16,19}(?!\d)"), "***"),     # 银行卡号
            (re.compile(r"sk-[a-zA-Z0-9]{20,}"),   "***"),      # API Key
        ]

    def check(self, context: dict[str, Any]) -> str | None:
        if context.get("type") not in ("output", "tool_output"):
            return None

        text = context.get("content", "")
        original = text

        for pattern, replacement in self.sensitive_patterns:
            text = pattern.sub(replacement, text)

        if text != original:
            text += "\n\n（注意：以上回复已自动屏蔽敏感信息）"

        return text
