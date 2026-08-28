from __future__ import annotations

from typing import Any

from harness.tools.base import BaseTool, ToolSpec


class PlanTool(BaseTool):
    """提案 / 问询工具：列出拟调用的工具并先向用户确认，不直接执行。

    设计：当操作需要用户确认、或缺失必填参数、或有副作用（写操作 / 外部副作用）时，
    模型应先调用 plan 列出将执行的工具（人类可读）并向用户提问；待用户回复后再输出
    带正确参数的最终执行列表。loop 拦截此工具：仅记录提案、把 message 交付用户、
    本轮结束并等待用户输入，不执行任何工具。
    """

    spec = ToolSpec(
        name="plan",
        description=(
            "在需要用户确认、缺参数或有副作用（写操作/外部副作用）前，先列出拟调用的工具"
            "并向用户提问。不要直接执行需要确认或有副作用的操作。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "actions": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "拟调用的工具的人类可读清单，如 ['查询订单 order_query', '提交售后 after_sale_apply']",
                },
                "message": {
                    "type": "string",
                    "description": "向用户提出的问题或确认语",
                },
            },
            "required": ["actions", "message"],
        },
    )

    async def run(self, actions: Any = None, message: str = "") -> str:
        # 实际不会被执行：loop 在工具分派前拦截 plan，仅记录提案并交付 message。
        return message or ""
