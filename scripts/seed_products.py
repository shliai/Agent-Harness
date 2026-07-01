"""将 data/products.json 追加入库 ChromaDB，自动生成 id 并使用本地 BGE 模型"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path

# 使用 HuggingFace 国内镜像源下载 BGE 模型
#os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")

import chromadb
from chromadb.config import Settings as ChromaSettings
from chromadb.utils.embedding_functions import SentenceTransformerEmbeddingFunction

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("seed_products")

COLLECTION_NAME = "ecommerce_knowledge"
MODEL_PATH = "models/bge-small-zh-v1.5"
DATA_PATH = "data/products.json"
CHROMA_PATH = "data/chroma_db"


def get_collection(client: chromadb.PersistentClient, embed_fn) -> chromadb.Collection:
    try:
        return client.get_collection(COLLECTION_NAME, embedding_function=embed_fn)
    except (ValueError, chromadb.errors.NotFoundError):
        return client.create_collection(COLLECTION_NAME, embedding_function=embed_fn)


def main() -> None:
    if not Path(DATA_PATH).exists():
        logger.error("商品数据文件不存在: %s", DATA_PATH)
        return

    with open(DATA_PATH, encoding="utf-8") as f:
        products: list[dict] = json.load(f)

    logger.info("读取 %d 条商品数据", len(products))

    embed_fn = SentenceTransformerEmbeddingFunction(model_name=MODEL_PATH)
    client = chromadb.PersistentClient(path=CHROMA_PATH, settings=ChromaSettings(anonymized_telemetry=False))
    collection = get_collection(client, embed_fn)

    exist_count = collection.count()
    logger.info("当前 ChromaDB 已有 %d 条记录", exist_count)

    ids: list[str] = []
    documents: list[str] = []
    metadatas: list[dict] = []

    for i, product in enumerate(products):
        ids.append(f"product_{exist_count + i:03d}")
        documents.append(json.dumps(product, ensure_ascii=False))
        metadatas.append({
            "category": product.get("category", ""),
            "brand": product.get("brand", ""),
            "price": product.get("price", 0),
        })

    collection.add(ids=ids, documents=documents, metadatas=metadatas)
    logger.info("追加成功: %d 条", len(ids))
    logger.info("ChromaDB 当前总计: %d 条记录", collection.count())


if __name__ == "__main__":
    main()
