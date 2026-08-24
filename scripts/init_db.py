"""初始化 SQLite 业务库并同步向量索引（数据源：data/seed/*.json）

用法：
    python scripts/init_db.py                # 建表 + 装载种子（幂等）
    python scripts/init_db.py --reset        # 先清除旧商品/订单/物流再装载
    python scripts/init_db.py --reindex      # 额外对账式重建向量索引
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.stdout.reconfigure(encoding="utf-8")

REPO = Path(__file__).resolve().parent.parent


def main() -> None:
    parser = argparse.ArgumentParser(description="初始化 SQLite 业务库")
    parser.add_argument("--reset", action="store_true",
                        help="装载前清空旧的 products/orders/logistics/aftersale 数据")
    parser.add_argument("--reindex", action="store_true", help="对账式重建向量索引（BGE 编码）")
    parser.add_argument("--prune-only", action="store_true", help="仅清理向量库脏 id")
    args = parser.parse_args()

    from harness.storage import db as store
    from harness.storage.seeds import load_logistics, load_orders, load_products

    store.init_schema()
    with store.db() as c:
        if args.reset:
            for t in ("products", "orders", "logistics", "aftersale"):
                c.execute(f"DELETE FROM {t}")
            print("old data cleared (--reset)")

        if c.execute("SELECT COUNT(*) FROM products").fetchone()[0] == 0 or args.reset:
            for p in load_products():
                store.upsert_product(c, p)
            print(f"products loaded: {len(load_products())}")
        else:
            print(f"products exist: {c.execute('SELECT COUNT(*) FROM products').fetchone()[0]} (skip)")

        if c.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 0 or args.reset:
            orders = load_orders()
            now = datetime.now().isoformat()
            c.executemany(
                """INSERT INTO orders(order_id,user_id,product_id,product_name,
                                     price,qty,discount_coupon,discount_promo,
                                     status,logistics_no,courier,created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                [(o["order_id"], o["user_id"], o["product_id"], o["product_name"],
                  o["price"], o["qty"], o.get("discount_coupon", 0),
                  o.get("discount_promo", 0), o["status"], o.get("logistics_no", ""),
                  o.get("courier", ""), o["created_at"]) for o in orders],
            )
            c.executemany(
                """INSERT INTO logistics(tracking_no,nodes_json,updated_at) VALUES(?,?,?)
                   ON CONFLICT(tracking_no) DO UPDATE SET nodes_json=excluded.nodes_json""",
                [(tno, json.dumps(nodes, ensure_ascii=False), now)
                 for tno, nodes in load_logistics().items()],
            )
            print(f"orders loaded: {len(orders)} | logistics: {len(load_logistics())}")
        else:
            print(f"orders exist (skip)")

    if args.prune_only:
        from harness.storage import vector_sync

        print("pruned:", vector_sync.reindex_all(prune=True))
        return

    if args.reindex:
        from harness.storage import vector_sync

        print("重建向量索引中…")
        print("reindexed:", vector_sync.reindex_all(prune=True))

    print("done.")


if __name__ == "__main__":
    main()
