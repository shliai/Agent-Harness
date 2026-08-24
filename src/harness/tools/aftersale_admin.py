"""商家侧售后审核业务逻辑（供 admin API 调用）

状态机唯一出口为 storage.db.update_aftersale_status；
本模块负责语义校验（存在性/合法流转/操作留痕）。
"""
from __future__ import annotations

from harness.storage import db as store


class AfterSaleNotFound(KeyError):
    pass


def _load(as_id: str) -> dict:
    rec = store.get_aftersale(as_id)
    if not rec:
        raise AfterSaleNotFound(as_id)
    return rec


def approve(as_id: str, operator: str = "admin", note: str = "") -> dict:
    """待审核 → 已通过"""
    rec = _load(as_id)
    if rec["status"] != "待审核":
        raise ValueError(f"仅待审核单可审批，当前状态：{rec['status']}")
    updated = store.update_aftersale_status(as_id, "已通过", operator, note)
    assert updated is not None
    return updated


def reject(as_id: str, reason: str, operator: str = "admin") -> dict:
    """待审核 → 已拒绝（须填写拒绝原因）"""
    rec = _load(as_id)
    if rec["status"] != "待审核":
        raise ValueError(f"仅待审核单可驳回，当前状态：{rec['status']}")
    reason = (reason or "").strip()
    if not reason:
        raise ValueError("驳回必须填写原因")
    return store.update_aftersale_status(as_id, "已拒绝", operator, reason)


def complete(as_id: str, operator: str = "admin") -> dict:
    """已通过 → 已完成（模拟退款打款成功）"""
    rec = _load(as_id)
    if rec["status"] != "已通过":
        raise ValueError(f"仅已通过单可完成打款，当前状态：{rec['status']}")
    return store.update_aftersale_status(as_id, "已完成", operator)


def list_for_admin(status: str | None = None, limit: int = 100) -> list[dict]:
    return store.list_aftersale_admin(status, limit)
