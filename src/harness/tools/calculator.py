from __future__ import annotations

import logging
from typing import Any

from harness.tools.base import BaseTool, ToolSpec

logger = logging.getLogger("harness.tools.calculator")


class CalculatorTool(BaseTool):
    """简单计算器工具"""

    spec = ToolSpec(
        name="calculator",
        description="执行数学计算，支持加减乘除、幂运算等",
        parameters={
            "type": "object",
            "properties": {
                "expression": {
                    "type": "string",
                    "description": "数学表达式，如 '2 + 3 * 4'",
                }
            },
            "required": ["expression"],
        },
    )

    async def run(self, **kwargs: Any) -> str:
        expression = kwargs.get("expression", "")
        if not expression.strip():
            return "请输入有效的数学表达式"

        try:
            # 安全评估：只允许数字和运算符
            allowed_chars = set("0123456789.+-*/()% ")
            if not all(c in allowed_chars for c in expression):
                return "表达式包含不允许的字符，仅支持数字和 + - * / ( ) %"

            result = eval(expression, {"__builtins__": {}}, {})
            logger.info("计算: %s = %s", expression, result)
            return str(result)
        except ZeroDivisionError:
            return "除数不能为0"
        except Exception as e:
            return f"计算错误: {e}"
