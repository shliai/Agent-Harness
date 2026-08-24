from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from typing import Any, Iterator

from harness.config import settings

_SCHEMA = """
CREATE TABLE IF NOT EXISTS products(
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    category    TEXT NOT NULL,
    brand       TEXT NOT NULL DEFAULT '',
    price       REAL NOT NULL DEFAULT 0,
    description TEXT NOT NULL DEFAULT '',
    specs_json  TEXT NOT NULL DEFAULT '{}',
    tags_json   TEXT NOT NULL DEFAULT '[]',
    status      TEXT NOT NULL DEFAULT '在售',
    updated_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_products_status ON products(status);

CREATE TABLE IF NOT EXISTS orders(
    order_id     TEXT PRIMARY KEY,
    user_id      TEXT NOT NULL,
    product_id   TEXT NOT NULL DEFAULT '',
    product_name TEXT NOT NULL,
    price        REAL NOT NULL DEFAULT 0,
    qty          INTEGER NOT NULL DEFAULT 1,
    discount_coupon REAL NOT NULL DEFAULT 0,
    discount_promo  REAL NOT NULL DEFAULT 0,
    status       TEXT NOT NULL,
    logistics_no TEXT NOT NULL DEFAULT '',
    courier      TEXT NOT NULL DEFAULT '',
    created_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_orders_user ON orders(user_id);
CREATE INDEX IF NOT EXISTS idx_orders_logistics ON orders(logistics_no);

CREATE TABLE IF NOT EXISTS logistics(
    tracking_no TEXT PRIMARY KEY,
    nodes_json  TEXT NOT NULL DEFAULT '[]',
    updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS aftersale(
    as_id       TEXT PRIMARY KEY,
    order_id    TEXT NOT NULL,
    user_id     TEXT NOT NULL,
    session_id  TEXT NOT NULL DEFAULT '',
    type        TEXT NOT NULL,
    reason      TEXT NOT NULL DEFAULT '',
    qty         INTEGER NOT NULL DEFAULT 0,
    refund_amount REAL NOT NULL DEFAULT 0,
    breakdown_json TEXT NOT NULL DEFAULT '{}',
    status      TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    history_json TEXT NOT NULL DEFAULT '[]'
);
CREATE INDEX IF NOT EXISTS idx_aftersale_user ON aftersale(user_id);

CREATE TABLE IF NOT EXISTS sessions(
    session_id           TEXT PRIMARY KEY,
    user_id              TEXT NOT NULL DEFAULT '',
    title                TEXT NOT NULL DEFAULT '',
    summary              TEXT NOT NULL DEFAULT '',
    working_memory_json  TEXT NOT NULL DEFAULT '{}',
    traces_json          TEXT NOT NULL DEFAULT '[]',
    updated_at           TEXT NOT NULL,
    created_at           TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sessions_updated ON sessions(updated_at);

CREATE TABLE IF NOT EXISTS session_messages(
    session_id  TEXT NOT NULL,
    seq         INTEGER NOT NULL,
    role        TEXT NOT NULL,
    content     TEXT NOT NULL,
    tool_call_id TEXT NOT NULL DEFAULT '',
    tool_name   TEXT NOT NULL DEFAULT '',
    timestamp   TEXT NOT NULL DEFAULT '',
    PRIMARY KEY(session_id, seq)
);
"""


_initialized_paths: set[str] = set()
_init_lock = __import__("threading").Lock()


# 存量库轻量迁移：缺失列自动补齐（ALTER 需逐条执行）
_EXPECTED_COLUMNS = {
    "products": {"stock": "INTEGER NOT NULL DEFAULT 0"},
    "orders": {"discount_coupon": "REAL NOT NULL DEFAULT 0",
               "discount_promo": "REAL NOT NULL DEFAULT 0"},
    "aftersale": {"qty": "INTEGER NOT NULL DEFAULT 0",
                  "refund_amount": "REAL NOT NULL DEFAULT 0",
                  "breakdown_json": "TEXT NOT NULL DEFAULT '{}'"},
}


def _migrate_columns(conn: sqlite3.Connection) -> None:
    for table, cols in _EXPECTED_COLUMNS.items():
        existing = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        for col, ddl in cols.items():
            if col not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}")


def _ensure_schema_for(path_str: str) -> None:
    with _init_lock:
        if path_str in _initialized_paths:
            return
        conn = sqlite3.connect(path_str, timeout=30)
        try:
            conn.executescript(_SCHEMA)
            _migrate_columns(conn)
            conn.commit()
        finally:
            conn.close()
        _initialized_paths.add(path_str)


def get_conn() -> sqlite3.Connection:
    settings.db_path.parent.mkdir(parents=True, exist_ok=True)
    path_str = str(settings.db_path)
    if path_str not in _initialized_paths:
        _ensure_schema_for(path_str)
    conn = sqlite3.connect(settings.db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


@contextmanager
def db() -> Iterator[sqlite3.Connection]:
    """每次操作独立短连接（WAL 模式下读写互不阻塞，线程安全最省心）"""
    conn = get_conn()
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_schema() -> None:
    with db() as c:
        c.executescript(_SCHEMA)


# ── 行转 dict 助手 ─────────────────────────────────────────

def _row_dict(row: sqlite3.Row | None) -> dict | None:
    return dict(row) if row is not None else None


def _rows_dicts(rows: list) -> list[dict]:
    return [dict(r) for r in rows]


# ── 商品 ───────────────────────────────────────────────────

def upsert_product(c: sqlite3.Connection, p: dict) -> None:
    from datetime import datetime

    c.execute(
        """INSERT INTO products(id,name,category,brand,price,description,specs_json,tags_json,status,stock,updated_at)
           VALUES(?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(id) DO UPDATE SET
             name=excluded.name, category=excluded.category, brand=excluded.brand,
             price=excluded.price, description=excluded.description,
             specs_json=excluded.specs_json, tags_json=excluded.tags_json,
             status=excluded.status, stock=excluded.stock, updated_at=excluded.updated_at""",
        (p["id"], p["name"], p["category"], p.get("brand", ""), float(p.get("price", 0)),
         p.get("description", ""), json.dumps(p.get("specs", {}), ensure_ascii=False),
         json.dumps(p.get("tags", []), ensure_ascii=False), p.get("status", "在售"),
         int(p.get("stock", 0)),
         datetime.now().isoformat(timespec="seconds")),
    )


def get_product(pid: str) -> dict | None:
    with db() as c:
        row = _row_dict(c.execute("SELECT * FROM products WHERE id=?", (pid,)).fetchone())
    if row:
        row["specs"] = json.loads(row.pop("specs_json"))
        row["tags"] = json.loads(row.pop("tags_json"))
    return row


def list_products(status: str | None = None) -> list[dict]:
    with db() as c:
        if status:
            rows = _rows_dicts(
                c.execute("SELECT * FROM products WHERE status=? ORDER BY id", (status,)).fetchall()
            )
        else:
            rows = _rows_dicts(c.execute("SELECT * FROM products ORDER BY id").fetchall())
    for r in rows:
        r["specs"] = json.loads(r.pop("specs_json"))
        r["tags"] = json.loads(r.pop("tags_json"))
    return rows


def delete_product(pid: str) -> bool:
    with db() as c:
        cur = c.execute("DELETE FROM products WHERE id=?", (pid,))
        return cur.rowcount > 0


# ── 订单 ───────────────────────────────────────────────────

def get_order(order_id: str) -> dict | None:
    with db() as c:
        return _row_dict(
            c.execute("SELECT * FROM orders WHERE order_id=?", (order_id,)).fetchone()
        )


def list_orders(user_id: str, status: str | None = None, limit: int = 10) -> list[dict]:
    sql = "SELECT * FROM orders WHERE user_id=?"
    args: list[Any] = [user_id]
    if status:
        sql += " AND status=?"
        args.append(status)
    sql += " ORDER BY created_at DESC LIMIT ?"
    args.append(limit)
    with db() as c:
        return _rows_dicts(c.execute(sql, args).fetchall())


# ── 物流 ───────────────────────────────────────────────────

def get_logistics(tracking_no: str) -> list[str] | None:
    with db() as c:
        row = c.execute("SELECT nodes_json FROM logistics WHERE tracking_no=?", (tracking_no,)).fetchone()
    return json.loads(row["nodes_json"]) if row else None


def put_logistics(tracking_no: str, nodes: list[str]) -> None:
    from datetime import datetime

    with db() as c:
        c.execute(
            """INSERT INTO logistics(tracking_no,nodes_json,updated_at) VALUES(?,?,?)
               ON CONFLICT(tracking_no) DO UPDATE SET nodes_json=excluded.nodes_json""",
            (tracking_no, json.dumps(nodes, ensure_ascii=False), datetime.now().isoformat()),
        )


# ── 售后 ───────────────────────────────────────────────────

def create_aftersale(record: dict) -> None:
    with db() as c:
        c.execute(
            """INSERT INTO aftersale(as_id,order_id,user_id,session_id,type,reason,
                                     qty,refund_amount,breakdown_json,status,
                                     created_at,updated_at,history_json)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (record["as_id"], record["order_id"], record["user_id"],
             record.get("session_id", ""), record["type"], record["reason"],
             record.get("refund_qty", 0), record.get("refund_amount", 0),
             json.dumps(record.get("breakdown", {}), ensure_ascii=False),
             record["status"], record["created_at"], record["updated_at"],
             json.dumps(record.get("history", []), ensure_ascii=False)),
        )


def find_active_aftersale(order_id: str, user_id: str) -> dict | None:
    with db() as c:
        row = _row_dict(c.execute(
            "SELECT * FROM aftersale WHERE order_id=? AND user_id=? AND status IN ('待审核','已通过')",
            (order_id, user_id),
        ).fetchone())
    return _hydrate_aftersale(row)


def list_aftersale(user_id: str, limit: int = 10) -> list[dict]:
    with db() as c:
        rows = _rows_dicts(c.execute(
            "SELECT * FROM aftersale WHERE user_id=? ORDER BY created_at DESC LIMIT ?",
            (user_id, limit),
        ).fetchall())
    return [_hydrate_aftersale(r) or r for r in rows]


def get_aftersale(as_id: str) -> dict | None:
    with db() as c:
        row = _row_dict(
            c.execute("SELECT * FROM aftersale WHERE as_id=?", (as_id,)).fetchone()
        )
    return _hydrate_aftersale(row)


def list_aftersale_admin(status: str | None = None, limit: int = 100) -> list[dict]:
    """商家侧视图：全部用户的售后单，可按状态筛选"""
    sql = "SELECT * FROM aftersale"
    args: list[Any] = []
    if status:
        sql += " WHERE status=?"
        args.append(status)
    sql += " ORDER BY updated_at DESC LIMIT ?"
    args.append(limit)
    with db() as c:
        rows = _rows_dicts(c.execute(sql, args).fetchall())
    return [_hydrate_aftersale(r) or r for r in rows]


def update_aftersale_status(
    as_id: str, to_status: str, operator: str = "system", note: str = ""
) -> dict | None:
    from datetime import datetime

    with db() as c:
        row = _row_dict(c.execute("SELECT * FROM aftersale WHERE as_id=?", (as_id,)).fetchone())
        if not row:
            return None
        history = json.loads(row.pop("history_json") or "[]")
        cur = row["status"]
        ts = datetime.now().isoformat(timespec="seconds")
        entry: dict[str, Any] = {"ts": ts, "from": cur, "to": to_status, "by": operator}
        if note:
            entry["note"] = note[:200]
        history.append(entry)
        c.execute(
            "UPDATE aftersale SET status=?, updated_at=?, history_json=? WHERE as_id=?",
            (to_status, ts, json.dumps(history, ensure_ascii=False), as_id),
        )
        row.update({"status": to_status, "updated_at": ts, "history": history})
        return row


def _hydrate_aftersale(row: dict | None) -> dict | None:
    if row and "breakdown_json" in row:
        row["breakdown"] = json.loads(row.pop("breakdown_json") or "{}")
        row["history"] = json.loads(row.pop("history_json") or "[]")
    return row
