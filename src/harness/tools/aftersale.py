from __future__ import annotations

import logging
import time
import uuid
from typing import Any

from harness.tools.base import BaseTool, ToolSpec

logger = logging.getLogger("harness.tools.aftersale")

# 售后单状态机：合法流转唯一出口，防止状态被随意跳转
STATUS_FLOW: dict[str, set[str]] = {
    "待审核": {"已通过", "已拒绝"},
    "已通过": {"已完成", "已取消"},
    "已取消": set(),
    "已拒绝": set(),
    "已完成": set(),
}

# 可发起售后的订单状态（待发货未履约，走取消/改地址而非售后）
AFTERSALE_ELIGIBLE_STATUS = {"已发货", "配送中", "已完成"}

_REFUND_TIMELINE = (
    "审核通过后寄回商品，仓库签收 48 小时内完成退款；"
    "原路退回，第三方支付 1-3 个工作日、银行卡 3-7 个工作日到账。"
)


def transition(record: dict, to_status: str, operator: str = "system") -> dict:
    """售后单状态流转（唯一入口）。非法流转抛 ValueError。"""
    cur = record["status"]
    if to_status not in STATUS_FLOW.get(cur, set()):
        raise ValueError(f"非法状态流转: {cur} → {to_status}")
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    record["status"] = to_status
    record["updated_at"] = ts
    record.setdefault("history", []).append({"ts": ts, "from": cur, "to": to_status, "by": operator})
    return record


def _append_record(record: dict) -> None:
    from harness.storage import db as store

    store.create_aftersale(record)



def calc_refund(order: dict, refund_qty: int) -> dict:
    """退款金额计算（口径对齐政策 POL-REFUND-03）

    资金模型：商品总价 price 为列表口径；实付 = price - 券抵扣 - 满减让利。
    规则：平台券按件均摊且抵扣部分不退；满减让利整单退时全额扣回，
    部分退货不扣回（剩余商品仍持有优惠）。
    """
    qty = max(int(order.get("qty") or 1), 1)
    refund_qty = max(1, min(int(refund_qty), qty))
    unit_list = float(order["price"]) / qty
    gross = round(unit_list * refund_qty, 2)
    coupon_share = round(float(order.get("discount_coupon") or 0) * refund_qty / qty, 2)
    promo_clawback = (
        round(float(order.get("discount_promo") or 0), 2) if refund_qty >= qty else 0.0
    )
    refund_amount = max(0.0, round(gross - coupon_share - promo_clawback, 2))
    return {
        "refund_qty": refund_qty,
        "gross": gross,
        "coupon_non_refundable": coupon_share,
        "promo_clawback": promo_clawback,
        "refund_amount": refund_amount,
        "paid_total": round(
            float(order["price"])
            - float(order.get("discount_coupon") or 0)
            - float(order.get("discount_promo") or 0),
            2,
        ),
    }


class AfterSaleApplyTool(BaseTool):
    spec = ToolSpec(
        name="after_sale_apply",
        description="为用户**提交**退货/换货申请（仅限本人订单，且订单已发货/配送中/已完成）。返回售后单号与后续流程说明。**注意：本工具仅用于发起新申请；查询已有售后进度请用 after_sale_query；退换政策咨询请用 policy_query。**",
        parameters={
            "type": "object",
            "properties": {
                "order_id": {"type": "string", "description": "订单编号"},
                "type": {"type": "string", "enum": ["退货", "换货"], "description": "售后类型"},
                "reason": {"type": "string", "description": "申请原因简述"},
                "qty": {"type": "integer", "description": "退货/换货件数，缺省为整单"},
            },
            "required": ["order_id", "type", "reason"],
        },
    )

    async def run(self, **kwargs: Any) -> str:
        import asyncio

        from harness.storage import db as store
        from harness.tools.context import current_session_id, current_user_id

        order_id = str(kwargs.get("order_id", "")).strip()
        apply_type = str(kwargs.get("type", "")).strip()
        reason = str(kwargs.get("reason", "")).strip() or "用户未填写"

        if apply_type not in ("退货", "换货"):
            return "售后类型只支持：退货 / 换货"

        order = await asyncio.to_thread(store.get_order, order_id)
        if not order:
            return f"未找到订单 {order_id}，请核对订单号"

        me = current_user_id.get()
        if order.get("user_id", "demo_user") != me:
            logger.warning("越权售后申请被拒绝: user=%s 订单=%s", me, order_id)
            return "该订单不属于当前账户，无法发起售后。如确为您本人订单请联系人工客服核实。"

        if order["status"] not in AFTERSALE_ELIGIBLE_STATUS:
            return (
                f"订单 {order_id} 当前状态为「{order['status']}」，暂不可申请售后。"
                "待发货订单如需变更请直接联系人工客服处理。"
            )

        # 幂等：同订单存在进行中的售后单则直接返回原单号
        from harness.storage import db as store

        existing = store.find_active_aftersale(order_id, me)
        if existing:
                return (
                    f"该订单已有进行中的售后申请（{existing['as_id']}，{existing['type']}，"
                    f"当前状态：{existing['status']}），无需重复提交。您可以随时让我查询售后进度。"
                )

        # 件数与退款测算
        try:
            refund_qty = int(kwargs.get("qty") or order["qty"])
        except (TypeError, ValueError):
            refund_qty = order["qty"]
        if not (1 <= refund_qty <= order["qty"]):
            return (
                f"退货件数须在 1~{order['qty']} 之间（该订单共 {order['qty']} 件），请确认后重新提交。"
            )

        breakdown = calc_refund(order, refund_qty)

        as_id = f"AS{uuid.uuid4().hex[:10].upper()}"
        now = time.strftime("%Y-%m-%d %H:%M:%S")
        record = {
            "as_id": as_id,
            "order_id": order_id,
            "user_id": me,
            "session_id": current_session_id.get(),
            "type": apply_type,
            "reason": reason[:200],
            "refund_qty": breakdown["refund_qty"],
            "refund_amount": breakdown["refund_amount"],
            "breakdown": breakdown,
            "status": "待审核",
            "created_at": now,
            "updated_at": now,
            "history": [{"ts": now, "from": "-", "to": "待审核", "by": "user"}],
        }
        _append_record(record)

        logger.info(
            "售后申请创建: %s | %s | %s | 退款预估 %.2f",
            as_id, order_id, apply_type, breakdown["refund_amount"],
        )
        timeline = _REFUND_TIMELINE if apply_type == "退货" else (
            "审核通过后寄回原商品，仓库验收合格 48 小时内寄出更换商品。"
        )
        money_lines = [f"退款测算：商品小计 ¥{breakdown['gross']}"]
        if breakdown["coupon_non_refundable"]:
            money_lines.append(f"　− 平台券分摊（不退）¥{breakdown['coupon_non_refundable']}")
        if breakdown["promo_clawback"]:
            money_lines.append(f"　− 满减让利扣回 ¥{breakdown['promo_clawback']}")
        money_lines.append(f"　= 预计退款 ¥{breakdown['refund_amount']}")

        return (
            f"售后申请已提交 ✓\n售后单号：{as_id}\n类型：{apply_type}\n"
            f"订单：{order_id}（{order['product_name']} × {breakdown['refund_qty']}/{order['qty']} 件）\n"
            + "\n".join(money_lines)
            + f"\n当前状态：待审核（预计 1-2 个工作日出结果）\n后续流程：{timeline}"
            f"\n您可以随时让我查询售后进度。"
        )


class AfterSaleQueryTool(BaseTool):
    spec = ToolSpec(
        name="after_sale_query",
        description="查询当前用户**已提交**的售后申请列表及各单状态进度。**仅用于查进度；退换货是否可行等政策问题请调用 policy_query；发起新申请请用 after_sale_apply。**",
        parameters={"type": "object", "properties": {}, "required": []},
    )

    async def run(self, **kwargs: Any) -> str:
        from harness.tools.context import current_user_id

        me = current_user_id.get()
        import asyncio

        from harness.storage import db as store

        rows = await asyncio.to_thread(store.list_aftersale, me, 10)

        if not rows:
            return "您名下暂无售后申请。如需退货或换货，告诉我订单号和问题即可为您提交。"

        lines = [f"您的售后申请（共 {len(rows)} 单）:"]
        for r in rows[:10]:
            amount = r.get("refund_amount") or 0
            amt_txt = f" | 预计退款 ¥{amount:.2f}" if amount else ""
            lines.append(
                f"- {r['as_id']} | {r['type']} | 订单 {r['order_id']} | "
                f"{r['status']}{amt_txt} | 申请时间 {r['created_at']}"
            )
        lines.append(f"\n时效参考：{_REFUND_TIMELINE}")
        return "\n".join(lines)
