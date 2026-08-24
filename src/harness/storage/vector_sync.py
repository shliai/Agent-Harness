from __future__ import annotations

import logging
from typing import Any

import chromadb
from chromadb.config import Settings as ChromaSettings

from harness.config import settings
from harness.memory.embeddings import get_embed_fn
from harness.storage import db as store

logger = logging.getLogger("harness.storage.vector_sync")

COLLECTION_NAME = "ecommerce_knowledge"

_client: chromadb.ClientAPI | None = None  # type: ignore[attr-defined]
_collection: Any | None = None


def get_collection() -> Any:
    """进程内共享 collection（与 KnowledgeRetrievalTool 同一实例语义）"""
    global _client, _collection
    if _collection is None:
        _client = chromadb.PersistentClient(
            path=str(settings.knowledge_store_path),
            settings=ChromaSettings(anonymized_telemetry=False),
        )
        ef = get_embed_fn()
        try:
            _collection = _client.get_collection(COLLECTION_NAME, embedding_function=ef)
        except (ValueError, chromadb.errors.NotFoundError):
            _collection = _client.create_collection(COLLECTION_NAME, embedding_function=ef)
    return _collection


def render_product_doc(p: dict) -> str:
    """商品 → 向量库文档的唯一渲染器（seed / 管理 API / 重索引共用，格式永不漂移）"""
    specs = p.get("specs") or {}
    tags = p.get("tags") or []
    specs_str = " ".join(f"{k}:{v}" for k, v in specs.items())
    tag_str = " ".join(tags)
    return (
        f"{p['name']} | {p.get('brand', '')}{p['category']} | ¥{p.get('price', 0)} | "
        f"{p.get('description', '')} | {specs_str} | 标签：{tag_str}"
    )


def product_metadata(p: dict) -> dict:
    return {
        "category": p["category"],
        "brand": p.get("brand", ""),
        "price": float(p.get("price", 0)),
        "status": p.get("status", "在售"),
        "stock": int(p.get("stock", 0)),
        "db_id": p["id"],
    }


def upsert_products(products: list[dict]) -> int:
    """批量入库/更新（阻塞含编码，调用方需放线程池）"""
    if not products:
        return 0
    coll = get_collection()
    coll.upsert(
        ids=[p["id"] for p in products],
        documents=[render_product_doc(p) for p in products],
        metadatas=[product_metadata(p) for p in products],
    )
    return len(products)


def delete_product(pid: str) -> None:
    """下架/删除时同步移除向量索引——修复「下架残留」缺口"""
    get_collection().delete(ids=[pid])


def reindex_all(prune: bool = True, batch: int = 64) -> dict:
    """对账式全量重建：以 SQLite 为事实源

    - DB 全量在售+下架商品都重嵌 upsert（模板或字段变更后调用）
    - prune=True 时删除向量库中不存在于 DB 的脏 id（历史遗留/手工写入）
    """
    products = store.list_products()
    coll = get_collection()

    total = 0
    for i in range(0, len(products), batch):
        total += upsert_products(products[i:i + batch])

    pruned = 0
    if prune:
        db_ids = {p["id"] for p in products}
        existing = set(coll.get(include=[])["ids"])
        stale = sorted(existing - db_ids)
        for j in range(0, len(stale), batch):
            coll.delete(ids=stale[j:j + batch])
            pruned += len(stale[j:j + batch])

    result = {"upserted": total, "pruned": pruned, "final_count": coll.count()}
    logger.info("向量库重建完成: %s", result)
    return result
