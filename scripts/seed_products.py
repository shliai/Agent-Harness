"""将 data/products.json 入库 ChromaDB，使用 upsert 模式避免重复数据"""
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

    # 使用稳定的确定性 id（基于 products.json 索引），配合 upsert 避免重复
    # 多次运行 seed 不会产生重复数据，已有记录会被覆盖更新
    ids: list[str] = []
    documents: list[str] = []
    metadatas: list[dict] = []

    for i, product in enumerate(products):
        ids.append(f"product_{i:03d}")
        # document 用自然语言拼接（含 description），让 BGE 语义检索更准确
        # JSON 字符串对 BGE 检索效果差，自然语言文本匹配度更高
        name = product.get("name", "")
        category = product.get("category", "")
        brand = product.get("brand", "")
        price = product.get("price", 0)
        description = product.get("description", "")
        tags = " ".join(product.get("tags", []))
        specs = product.get("specs", {})
        specs_str = " ".join(f"{k}:{v}" for k, v in specs.items())
        document = f"{name} | {brand}{category} | ¥{price} | {description} | {specs_str} | 标签：{tags}"
        documents.append(document)
        metadatas.append({
            "category": category,
            "brand": brand,
            "price": price,
        })

    # upsert：存在则更新，不存在则插入，幂等操作
    collection.upsert(ids=ids, documents=documents, metadatas=metadatas)
    logger.info("upsert 成功: %d 条", len(ids))
    logger.info("ChromaDB 当前总计: %d 条记录", collection.count())


if __name__ == "__main__":
    main()

