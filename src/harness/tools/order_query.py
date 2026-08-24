from __future__ import annotations

import logging
from typing import Any

from harness.tools.base import BaseTool, ToolSpec

logger = logging.getLogger("harness.tools.order_query")

class OrderQueryTool(BaseTool):
    spec = ToolSpec(
        name="order_query",
        description="根据订单号查询当前用户的订单详情，包括商品名称、价格、状态、物流信息等。只能查询本人订单",
        parameters={
            "type": "object",
            "properties": {
                "order_id": {
                    "type": "string",
                    "description": "订单编号，如 20240601001",
                }
            },
            "required": ["order_id"],
        },
    )

    def __init__(self) -> None:
        from harness.tools.context import EnumerationGuard

        self._guard = EnumerationGuard(max_misses=8, window_seconds=900, cooldown_seconds=1800)

    async def run(self, **kwargs: Any) -> str:
        from harness.tools.context import current_session_id, current_user_id

        import re as _re

        order_id = str(kwargs.get("order_id", "")).strip()
        if not order_id:
            return "请输入订单号"
        key = f"order_query:{current_session_id.get() or 'anonymous'}"
        blocked = self._guard.check(key)
        if blocked:
            logger.warning("订单查询熔断中: %s", key)
            return blocked

        # LLM 抽参白名单校验：阻断畸形参数直达查询层
        if not _re.fullmatch(r"[0-9]{11,15}", order_id):
            self._guard.record_miss(f"order_query:{current_session_id.get() or 'a'}")
            return f"订单号 {order_id} 格式不正确，应为下单日期开头的数字编号，请核对后重试"

        import asyncio

        from harness.storage import db as store

        order = await asyncio.to_thread(store.get_order, order_id)
        if not order:
            self._guard.record_miss(key)
            return f"未找到订单 {order_id}，请核对订单号后重试"

        # 归属校验：只能查询本人的订单
        owner = order["user_id"]
        me = current_user_id.get()
        if owner != me:
            logger.warning("越权查询被拒绝: user=%s 订单=%s 归属=%s", me, order_id, owner)
            return (
                f"订单 {order_id} 不属于当前账户，无法查询。"
                "请确认订单号是否输入正确；如确为您的订单但无法查看，请联系人工客服核实身份。"
            )

        self._guard.record_hit(key)

        lines = [
            f"订单号：{order['order_id']}",
            f"商品：{order['product_name']}",
            f"金额：¥{order['price']}",
            f"状态：{order['status']}",
            f"下单时间：{order['created_at']}",
        ]
        if order["logistics_no"]:
            lines.append(f"物流单号：{order['logistics_no']}")
            lines.append(f"快递公司：{order['courier']}")

        logger.info("订单查询: %s -> %s", order_id, order["status"])
        return "\n".join(lines)


class MyOrdersTool(BaseTool):
    """按当前用户列出订单（用户不记得单号时的入口）"""

    spec = ToolSpec(
        name="order_list",
        description="查询当前用户的订单列表（最近下单优先），用于用户不记得订单号时先列出订单再选择",
        parameters={
            "type": "object",
            "properties": {
                "status": {
                    "type": "string",
                    "description": "可选，按状态筛选：待发货/已发货/配送中/已完成",
                }
            },
            "required": [],
        },
    )

    async def run(self, **kwargs: Any) -> str:
        import asyncio

        from harness.storage import db as store
        from harness.tools.context import current_user_id

        me = current_user_id.get()
        status_filter = str(kwargs.get("status", "")).strip() or None

        rows = await asyncio.to_thread(store.list_orders, me, status_filter, 10)
        if not rows:
            return "当前账户名下没有符合条件的订单"

        lines = [f"您的订单（最近优先，最多显示 10 单）:"]
        for o in rows:
            lines.append(
                f"- {o['order_id']} | {o['product_name']} | ¥{o['price']} | "
                f"{o['status']} | 下单于 {o['created_at']}"
            )
        return "\n".join(lines)
