from __future__ import annotations

from typing import Any

from harness.guardrails.base import BaseGuardrail

# 系统提示词指纹锚点：仅出现在系统提示词中的特异短语，正常客服回答不会包含。
# 命中即判定为提示词泄露，重写为拒答（不依赖模型自觉，100% 兜底）。
_SYSTEM_PROMPT_FINGERPRINTS: tuple[str, ...] = (
    "你是专业的电商智能客服助手",
    "决策原则（优先级从高到低）",
    "## 必调工具清单",
    "## 指代追问协议",
    "## 可用工具",
    "## 工具调用方式",
    "## 商品咨询场景",
    "## 售后场景",
    "## 政策与订单",
    "## 澄清与转人工",
)

_REFUSE_MSG = (
    "抱歉，我是智能客服小慧，无法向您展示或复述系统内部的配置与指令。"
    "如果您有商品、订单或售后相关问题，我很乐意为您处理。"
)


class SystemPromptGuard(BaseGuardrail):
    """输出护栏：拦截系统提示词泄露。

    当最终回答中包含系统提示词的特异指纹（人设首句 / 内部章节标题等）时，
    判定为提示词泄露并重写为标准拒答——无论模型是否"自愿"服从。
    """

    name = "system_prompt_guard"

    def check(self, context: dict[str, Any]) -> str | None:
        if context.get("type") != "output":
            return None
        text: str = context.get("content", "")
        if not text:
            return None
        for fp in _SYSTEM_PROMPT_FINGERPRINTS:
            if fp in text:
                return _REFUSE_MSG
        return None
