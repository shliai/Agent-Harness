from __future__ import annotations

import ast
import logging
import math
from typing import Any

from harness.tools.base import BaseTool, ToolSpec

logger = logging.getLogger("harness.tools.calculator")

# 防御性上限：避免 9**99999999 这类表达式长时间占用 CPU
MAX_EXPRESSION_LENGTH = 200
MAX_OPERAND = 1e15
MAX_POW_EXPONENT = 1000
MAX_RESULT = 1e308


class _SafeEvaluator(ast.NodeVisitor):
    """白名单 AST 求值器：仅允许数字与四则/幂/取模运算，无任何名称访问"""

    def visit(self, node: ast.AST) -> float:
        if isinstance(node, ast.Expression):
            return self.visit(node.body)
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
            value = float(node.value)
            if not math.isfinite(value):
                raise ValueError("数字必须是有限值")
            return value
        if isinstance(node, ast.BinOp):
            return self._visit_binop(node)
        if isinstance(node, ast.UnaryOp):
            value = self.visit(node.operand)
            if isinstance(node.op, ast.UAdd):
                return value
            if isinstance(node.op, ast.USub):
                return -value
            raise ValueError(f"不支持的一元运算符: {type(node.op).__name__}")
        raise ValueError(f"不支持的表达式元素: {type(node).__name__}")

    def _visit_binop(self, node: ast.BinOp) -> float:
        left = self.visit(node.left)
        right = self.visit(node.right)

        if isinstance(node.op, ast.Add):
            return left + right
        if isinstance(node.op, ast.Sub):
            return left - right
        if isinstance(node.op, ast.Mult):
            self._check_operands(left, right)
            return left * right
        if isinstance(node.op, ast.Div):
            if right == 0:
                raise ZeroDivisionError()
            return left / right
        if isinstance(node.op, ast.FloorDiv):
            if right == 0:
                raise ZeroDivisionError()
            return math.floor(left / right)
        if isinstance(node.op, ast.Mod):
            if right == 0:
                raise ZeroDivisionError()
            return math.fmod(left, right)
        if isinstance(node.op, ast.Pow):
            # 幂运算是 DoS 重灾区：限制指数与底数规模
            if abs(right) > MAX_POW_EXPONENT or abs(left) > 1e6:
                raise ValueError("幂运算的数值超出允许范围")
            result = math.pow(left, right)
            if not math.isfinite(result) or abs(result) > MAX_RESULT:
                raise ValueError("运算结果溢出")
            return result
        raise ValueError(f"不支持的运算符: {type(node.op).__name__}")

    @staticmethod
    def _check_operands(left: float, right: float) -> None:
        if abs(left) > MAX_OPERAND or abs(right) > MAX_OPERAND:
            raise ValueError("参与运算的数值过大")


class CalculatorTool(BaseTool):
    """安全计算器：AST 白名单求值，不使用 eval"""

    spec = ToolSpec(
        name="calculator",
        description="执行数学计算，支持加减乘除、整除、取余、幂运算和括号。**涉及金额、折扣、优惠、总价或任何含数字的表达式计算时，必须调用本工具，不要自行心算。**",
        parameters={
            "type": "object",
            "properties": {
                "expression": {
                    "type": "string",
                    "description": "数学表达式，如 '2 + 3 * 4'、'(1500 + 2500) * 0.85'",
                }
            },
            "required": ["expression"],
        },
    )

    async def run(self, **kwargs: Any) -> str:
        expression = str(kwargs.get("expression", "")).strip()
        if not expression:
            return "请输入有效的数学表达式"

        if len(expression) > MAX_EXPRESSION_LENGTH:
            return f"表达式过长（最多 {MAX_EXPRESSION_LENGTH} 字符）"

        try:
            tree = ast.parse(expression, mode="eval")
        except (SyntaxError, ValueError, MemoryError, RecursionError):
            return "表达式格式错误，仅支持数字和 + - * / // % ** ( )"

        try:
            result = _SafeEvaluator().visit(tree)
        except ZeroDivisionError:
            return "除数不能为0"
        except ValueError as e:
            return f"计算被拒绝: {e}"
        except (RecursionError, MemoryError):
            return "表达式过于复杂"
        except Exception as e:  # 兜底：任何求值异常都不应中断 Agent 循环
            return f"计算错误: {e}"

        logger.info("计算: %s = %s", expression, result)

        # 整数结果去掉小数点尾巴（2.0 -> 2），浮点保留合理精度
        if isinstance(result, float) and result.is_integer() and abs(result) < 1e15:
            return str(int(result))
        return f"{result:.10g}"
