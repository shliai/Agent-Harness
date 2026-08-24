from __future__ import annotations

import re
from typing import Any

from harness.domain.exceptions import InputValidationError
from harness.guardrails.base import BaseGuardrail

# 指令注入特征（中英双语，演示级词表；生产建议接分类模型）
_INJECTION_PATTERNS = [
    re.compile(r"忽略.{0,6}(指令|规则|设定|提示词?|提示语)", re.I),
    re.compile(r"(系统提示词?|system\s*prompt).{0,8}(打印|输出|显示|泄露)", re.I),
    re.compile(r"ignore\s+(all\s+)?(previous|above|prior)\s+(instructions|rules|prompts)", re.I),
    re.compile(r"(reveal|show|print).{0,8}(system\s*prompt)", re.I),
    re.compile(r"(你现在是|现在扮演)[^。]{0,20}"
                   r"(开发者模式|管理员|系统权限|DAN|越狱)", re.I),
    re.compile(r"jailbreak|DAN模式|越狱", re.I),
]


class InjectionGuard(BaseGuardrail):
    """Prompt 注入防护：命中特征即拦截并审计留痕（可经配置关闭）"""

    name = "injection_guard"

    def check(self, context: dict[str, Any]) -> str | None:
        from harness.config import settings

        if context.get("type") != "input" or not settings.prompt_injection_block:
            return None
        text = str(context.get("content", ""))
        for pat in _INJECTION_PATTERNS:
            if pat.search(text):
                raise InputValidationError(
                    "检测到疑似指令注入内容，该请求已被安全策略拦截。"
                )
        return None
