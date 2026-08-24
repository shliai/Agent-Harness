from __future__ import annotations

import pytest

from harness.domain.exceptions import ToolNotFoundError
from harness.tools.calculator import CalculatorTool
from harness.core.registry import Registry


class TestCalculatorTool:
    @pytest.mark.asyncio
    async def test_simple_calculation(self) -> None:
        tool = CalculatorTool()
        result = await tool.run(expression="2 + 3")
        assert result == "5"

    @pytest.mark.asyncio
    async def test_division_by_zero(self) -> None:
        tool = CalculatorTool()
        result = await tool.run(expression="1/0")
        assert "除数不能为0" in result

    @pytest.mark.asyncio
    async def test_invalid_expression(self) -> None:
        tool = CalculatorTool()
        result = await tool.run(expression="__import__('os')")
        assert "拒绝" in result or "不支持" in result


class TestToolRegistry:
    def test_register_and_get(self) -> None:
        registry = Registry()
        tool = CalculatorTool()
        registry.register_tool(tool)
        assert registry.get_tool("calculator") is tool

    def test_get_nonexistent(self) -> None:
        registry = Registry()
        with pytest.raises(ToolNotFoundError):
            registry.get_tool("nonexistent")

    def test_list(self) -> None:
        registry = Registry()
        registry.register_tool(CalculatorTool())
        assert "calculator" in registry.list_tools()
