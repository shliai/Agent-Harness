"""v0.6.0 商家审核侧 + 退款计算测试"""
from __future__ import annotations

import pytest

from harness.tools.aftersale_admin import (
    AfterSaleNotFound,
    approve,
    complete,
    list_for_admin,
    reject,
)
from harness.tools.aftersale import calc_refund


# ── 退款计算 ───────────────────────────────────────────────

class TestRefundCalc:
    ORDER = {
        "order_id": "X1", "price": 600.0, "qty": 2,
        "discount_coupon": 100.0, "discount_promo": 50.0,  # 实付 450
    }

    def test_full_return_with_clawback(self) -> None:
        """整单退：退商品小计 − 券不退 − 满减全额扣回"""
        b = calc_refund(self.ORDER, refund_qty=2)
        assert b["gross"] == 600.0
        assert b["coupon_non_refundable"] == 100.0
        assert b["promo_clawback"] == 50.0
        assert b["refund_amount"] == 450.0
        assert b["paid_total"] == 450.0

    def test_partial_return_no_clawback(self) -> None:
        """部分退：按件均摊券，满减不扣回"""
        b = calc_refund(self.ORDER, refund_qty=1)
        assert b["gross"] == 300.0
        assert b["coupon_non_refundable"] == 50.0
        assert b["promo_clawback"] == 0.0
        assert b["refund_amount"] == 250.0

    def test_no_discounts(self) -> None:
        o = {"order_id": "X2", "price": 999.0, "qty": 3}
        b = calc_refund(o, refund_qty=3)
        assert b["refund_amount"] == 999.0 and b["paid_total"] == 999.0

    def test_clamped_non_negative(self) -> None:
        """极端优惠超过货值时退款不为负"""
        o = {"order_id": "X3", "price": 100.0, "qty": 1,
             "discount_coupon": 80.0, "discount_promo": 50.0}
        b = calc_refund(o, refund_qty=1)
        assert b["refund_amount"] == 0.0

    def test_qty_clamped(self) -> None:
        b = calc_refund({"order_id": "X", "price": 300.0, "qty": 2}, refund_qty=99)
        assert b["refund_qty"] == 2


# ── 商家审核状态机（端到端：申请 → 审批 → 打款）─────────────

class TestMerchantReviewFlow:
    @pytest.fixture
    def applied(self, seeded_db, monkeypatch):
        from harness.tools.context import current_user_id

        current_user_id.set(seeded_db.demo_user)
        own = next(
            o for o in seeded_db.demo_orders
            if o["status"] in ("已发货", "配送中", "已完成")
        )
        from harness.tools.aftersale import AfterSaleApplyTool

        out = asyncio_run_apply(own["order_id"])
        as_id = out.split("售后单号：AS")[1][:10]
        as_id = "AS" + as_id
        return seeded_db, as_id

    @pytest.mark.asyncio
    async def test_approve_then_complete(self, applied) -> None:
        _, as_id = applied
        row = approve(as_id, operator="boss", note="凭证齐全")
        assert row["status"] == "已通过"

        row = complete(as_id, operator="finance")
        assert row["status"] == "已完成"
        assert row["history"][-1]["by"] == "finance"
        assert row["history"][-2]["note"] == "凭证齐全"

    @pytest.mark.asyncio
    async def test_reject_requires_reason(self, applied) -> None:
        _, as_id = applied
        with pytest.raises(ValueError):
            reject(as_id, reason="")
        row = reject(as_id, reason="超过7天无理由时限")
        assert row["status"] == "已拒绝"
        with pytest.raises(ValueError):
            approve(as_id)  # 已拒绝不可再审

    @pytest.mark.asyncio
    async def test_illegal_transition_blocked(self, applied) -> None:
        _, as_id = applied
        approve(as_id)
        with pytest.raises(ValueError):
            approve(as_id)  # 重复审批

    def test_admin_list_filter(self, applied) -> None:
        _, as_id = applied
        pending = list_for_admin("待审核")
        assert any(r["as_id"] == as_id for r in pending)

    def test_not_found(self) -> None:
        with pytest.raises(AfterSaleNotFound):
            approve("AS_NOT_EXIST")


def asyncio_run_apply(order_id: str) -> str:
    import asyncio

    from harness.tools.aftersale import AfterSaleApplyTool

    return asyncio.run(AfterSaleApplyTool().run(order_id=order_id, type="退货", reason="评测"))
