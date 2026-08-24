"""种子数据装载器：data/seed/*.json 是商品/订单/物流的唯一策划事实源"""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

SEED_DIR = Path(__file__).resolve().parents[3] / "data" / "seed"

DEMO_USER = "demo_user"
SECOND_USER = "user_b"


@lru_cache(maxsize=1)
def load_products() -> list[dict]:
    return json.loads((SEED_DIR / "products.json").read_text(encoding="utf-8"))


@lru_cache(maxsize=1)
def load_orders() -> list[dict]:
    return json.loads((SEED_DIR / "orders.json").read_text(encoding="utf-8"))


@lru_cache(maxsize=1)
def load_logistics() -> dict[str, list[str]]:
    """返回 {tracking_no: [轨迹行]}，与历史 build_orders 契约一致"""
    rows = json.loads((SEED_DIR / "logistics.json").read_text(encoding="utf-8"))
    return {r["tracking_no"]: r["nodes"] for r in rows}
