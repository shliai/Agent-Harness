"""v0.4 存储层测试：SQLite CRUD / 向量同步 prune / 管理员鉴权"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest


class TestProductRepo:
    def test_upsert_and_get_roundtrip(self, tmp_path, monkeypatch) -> None:
        from harness.config import settings
        from harness.storage import db as store
        from harness.storage.seeds import load_products as build_products

        monkeypatch.setattr(settings, "db_path", tmp_path / "t.db")
        store.init_schema()

        p = build_products()[0]
        with store.db() as c:
            store.upsert_product(c, p)

        got = store.get_product(p["id"])
        assert got["name"] == p["name"] and got["specs"] == p["specs"] and got["tags"] == p["tags"]

        # 更新价格（upsert 幂等覆盖）
        p2 = {**p, "price": p["price"] + 100}
        with store.db() as c:
            store.upsert_product(c, p2)
        assert store.get_product(p["id"])["price"] == p2["price"]

    def test_delete_and_list_status(self, tmp_path, monkeypatch) -> None:
        from harness.config import settings
        from harness.storage import db as store
        from harness.storage.seeds import load_products as build_products

        monkeypatch.setattr(settings, "db_path", tmp_path / "t.db")
        store.init_schema()
        ps = build_products()[:3]
        with store.db() as c:
            for p in ps:
                store.upsert_product(c, p)

        assert len(store.list_products("在售")) == 3
        assert store.delete_product(ps[0]["id"]) is True
        assert store.get_product(ps[0]["id"]) is None
        assert len(store.list_products()) == 2


class TestOrderLogisticsRepo:
    def test_order_query_paths(self, seeded_db) -> None:
        from harness.storage import db as store

        o = seeded_db.demo_orders[0]
        got = store.get_order(o["order_id"])
        assert got["user_id"] == o["user_id"] and got["product_name"] == o["product_name"]
        assert store.get_order("no-such") is None

        mine = store.list_orders(seeded_db.demo_user, limit=100)
        assert all(r["user_id"] == seeded_db.demo_user for r in mine)

    def test_logistics_roundtrip(self, seeded_db) -> None:
        from harness.storage import db as store

        tno = next(iter(seeded_db.logistics))
        nodes = store.get_logistics(tno)
        assert nodes and nodes[0].count(" ") >= 2


class TestVectorSync:
    def test_render_format_stable(self) -> None:
        from harness.storage.vector_sync import product_metadata, render_product_doc

        p = {"id": "product_x", "name": "测试机", "category": "手机", "brand": "X牌",
             "price": 1999, "description": "好用的手机", "specs": {"屏幕": "6寸"},
             "tags": ["性价比"], "status": "在售"}
        doc = render_product_doc(p)
        for token in ["测试机", "X牌手机", "¥1999", "好用的手机", "屏幕:6寸", "#"]:
            pass
        assert doc.startswith("测试机 | X牌手机 | ¥1999")
        assert "标签：性价比" in doc
        md = product_metadata(p)
        assert md["category"] == "手机" and md["status"] == "在售" and md["price"] == 1999

    def test_reindex_prunes_stale_ids(self, monkeypatch) -> None:
        """DB 为事实源：向量库中的脏 id 必须被 prune 掉"""
        from harness.storage import vector_sync

        coll = MagicMock()
        coll.get.return_value = {"ids": ["product_001", "stale_old_id"]}
        coll.count.return_value = 1
        captured = {}

        def fake_delete(ids):
            captured["deleted"] = ids

        coll.delete.side_effect = fake_delete
        monkeypatch.setattr(vector_sync, "get_collection", lambda: coll)
        monkeypatch.setattr(vector_sync, "store", vector_sync.store)

        rows = [{"id": "product_001", "name": "A", "category": "手机", "brand": "",
                 "price": 100, "description": "", "specs": {}, "tags": [], "status": "在售"}]
        monkeypatch.setattr(
            vector_sync.store, "list_products", lambda status=None: rows
        )

        r = vector_sync.reindex_all(prune=True)
        assert r["pruned"] == 1 and captured["deleted"][-1] == "stale_old_id"


class TestAdminAuth:
    def test_require_admin(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from fastapi import HTTPException

        from harness.config import settings
        from harness.web.api import require_admin

        monkeypatch.setattr(settings, "admin_token", "t-secret-123")
        require_admin("t-secret-123")  # 配置的 token 通过
        with pytest.raises(HTTPException):
            require_admin(None)
        with pytest.raises(HTTPException):
            require_admin("wrong")

    def test_require_admin_fail_closed_when_unconfigured(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """ADMIN_TOKEN 未配置时无论传入什么都拒绝（无默认凭据）"""
        from fastapi import HTTPException

        from harness.config import settings
        from harness.web.api import require_admin

        monkeypatch.setattr(settings, "admin_token", "")
        with pytest.raises(HTTPException):
            require_admin(None)
        with pytest.raises(HTTPException):
            require_admin("")
        with pytest.raises(HTTPException):
            require_admin("demo-admin-token")
