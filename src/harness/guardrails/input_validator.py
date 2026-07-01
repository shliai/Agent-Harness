from __future__ import annotations

import re
from typing import Any

from harness.domain.exceptions import InputValidationError
from harness.guardrails.base import BaseGuardrail


class InputValidator(BaseGuardrail):
    """输入校验：拦截空内容、过长内容、可疑注入"""

    name = "input_validator"

    def __init__(self, max_length: int = 4096) -> None:
        self.max_length = max_length

    def check(self, context: dict[str, Any]) -> str | None:
        if context.get("type") != "input":
            return None

        text = context.get("content", "")
        if not text.strip():
            raise InputValidationError("输入不能为空")

        if len(text) > self.max_length:
            raise InputValidationError(f"输入超过最大长度限制 ({self.max_length} 字符)")

        if re.search(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", text):
            raise InputValidationError("输入包含非法控制字符")

        return None
