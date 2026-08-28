from __future__ import annotations

from harness.tools.base import BaseTool, ToolSpec


class RespondTool(BaseTool):
    """最终回复工具：承载「终态」——把自然语言回复交付用户。

    设计上每轮模型都必须输出工具调用（文本协议已移除）。模型不得直接输出裸文本，
    而是调用 respond 承载最终回复。loop 在工具分派前拦截 respond，以其 content
    作为 AgentResult.answer，不实际执行任何动作。
    """

    spec = ToolSpec(
        name="respond",
        description=(
            "输出最终回复给用户。每轮都必须调用某个工具，最终回答用本工具承载；"
            "若需向用户提问也可用本工具（但凡涉及需要确认/缺参数的操作，优先用 plan）。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "content": {
                    "type": "string",
                    "description": "回复用户的最终自然语言内容",
                }
            },
            "required": ["content"],
        },
    )

    async def run(self, content: str = "") -> str:
        # 实际不会被执行：loop 在工具分派前拦截 respond，仅将其 content 作为答案。
        return content or ""
