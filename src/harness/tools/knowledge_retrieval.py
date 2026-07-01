from __future__ import annotations

import json
import logging
import math
import os
import re
from collections import Counter
from typing import Any

# 使用 HuggingFace 国内镜像源下载 BGE 模型
#os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")

import chromadb
from chromadb.config import Settings as ChromaSettings

from harness.config import settings
from harness.domain.exceptions import ToolExecutionError
from harness.tools.base import BaseTool, ToolSpec

logger = logging.getLogger("harness.tools.knowledge_retrieval")

COLLECTION_NAME = "ecommerce_knowledge"
MODEL_PATH = "models/bge-small-zh-v1.5"

KNOWN_CATEGORIES = {"手机", "笔记本", "耳机", "平板", "穿戴", "配件"}


class BM25:
    def __init__(self, docs: list[str], k1: float = 1.5, b: float = 0.75) -> None:
        self.k1 = k1
        self.b = b
        self.avgdl = sum(len(d.split()) for d in docs) / max(len(docs), 1)
        self.corpus_size = len(docs)
        self.doc_freqs: list[Counter] = [Counter(d.split()) for d in docs]
        self.idf: dict[str, float] = {}
        df = Counter()
        for doc_counter in self.doc_freqs:
            for term in doc_counter:
                df[term] += 1
        for term, count in df.items():
            self.idf[term] = math.log(1 + (self.corpus_size - count + 0.5) / (count + 0.5))

    def score(self, query: str, doc_index: int) -> float:
        score = 0.0
        query_terms = query.split()
        doc_counter = self.doc_freqs[doc_index]
        doc_len = sum(doc_counter.values())
        for term in query_terms:
            if term not in self.idf:
                continue
            tf = doc_counter.get(term, 0)
            score += self.idf[term] * (tf * (self.k1 + 1)) / (tf + self.k1 * (1 - self.b + self.b * doc_len / self.avgdl))
        return score


class KnowledgeRetrievalTool(BaseTool):
    """知识库检索工具：ChromaDB + 本地 BGE + BM25 混合检索"""

    spec = ToolSpec(
        name="knowledge_retrieval",
        description="检索电商商品知识库，用于回答商品信息、价格查询、参数对比、商品推荐等业务问题",
        parameters={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "用户的问题，如'5000元以下的拍照手机'",
                }
            },
            "required": ["query"],
        },
    )

    def __init__(self) -> None:
        self.collection: chromadb.Collection | None = None
        self._embed_fn = None
        try:
            self.client = chromadb.PersistentClient(
                path=str(settings.knowledge_store_path),
                settings=ChromaSettings(anonymized_telemetry=False),
            )
            self._embed_fn = self._build_embed_fn()
            self.collection = self._get_or_create_collection()
            logger.info("KnowledgeRetrievalTool 初始化完成 (ChromaDB + BGE + BM25)")
        except Exception as e:
            logger.warning("知识库初始化失败，降级运行: %s", e)

    @staticmethod
    def _build_embed_fn():
        try:
            from chromadb.utils.embedding_functions import SentenceTransformerEmbeddingFunction
            return SentenceTransformerEmbeddingFunction(model_name=MODEL_PATH)
        except Exception as e:
            logger.warning("嵌入模型加载失败: %s", e)
            return None

    def _get_or_create_collection(self) -> chromadb.Collection | None:
        ef = self._embed_fn or None
        try:
            return self.client.get_collection(COLLECTION_NAME, embedding_function=ef)
        except (ValueError, chromadb.errors.NotFoundError):
            return self.client.create_collection(COLLECTION_NAME, embedding_function=ef)
        except Exception as e:
            logger.warning("无法获取或创建 ChromaDB 集合: %s", e)
            return None

    @staticmethod
    def _extract_filters(query: str) -> dict[str, Any]:
        filters: dict[str, Any] = {}

        m = re.search(r"(\d+)\s*元?以[下内]", query)
        if m:
            filters["price_max"] = float(m.group(1))

        m = re.search(r"(\d+)\s*[-~到至]\s*(\d+)\s*元", query)
        if m:
            filters["price_min"] = float(m.group(1))
            filters["price_max"] = float(m.group(2))

        m = re.search(r"(\d+)\s*元以上", query)
        if m:
            filters["price_min"] = float(m.group(1))

        for cat in KNOWN_CATEGORIES:
            if cat in query:
                filters["category"] = cat
                break

        return filters

    @staticmethod
    def _format_product(product: dict) -> str:
        name = product.get("name", "未知")
        price = product.get("price", 0)
        category = product.get("category", "")
        brand = product.get("brand", "")
        specs = product.get("specs", {})
        spec_str = " ".join(f"{k}:{v}" for k, v in specs.items())
        tags = product.get("tags", [])
        tag_str = " ".join(f"#{t}" for t in tags) if tags else ""
        return f"{name} | {brand} {category} | ¥{price} | {spec_str} {tag_str}".strip()

    async def run(self, **kwargs: Any) -> str:
        user_query = kwargs.get("query", "")
        if not user_query.strip():
            return "请输入有效的问题"

        if self.collection is None:
            return "知识库未就绪，请联系管理员导入数据"

        try:
            count = self.collection.count()
            if count == 0:
                return "知识库为空，暂无商品信息"

            filters = self._extract_filters(user_query)
            where_clause = self._build_where(filters)

            vector_results = self.collection.query(
                query_texts=[user_query],
                n_results=count,
                where=where_clause,
            )

            all_docs: list[dict] = []
            docs_list = vector_results.get("documents", [[]])[0]
            metas_list = vector_results.get("metadatas", [[]])[0]
            dists_list = vector_results.get("distances", [[]])[0]
            ids_list = vector_results.get("ids", [[]])[0]

            for doc, meta, dist, doc_id in zip(docs_list, metas_list or [], dists_list or [], ids_list or []):
                all_docs.append({
                    "id": doc_id,
                    "document": doc,
                    "metadata": meta or {},
                    "distance": dist,
                })

            if not all_docs:
                return "暂无匹配的商品"

            bm25 = BM25([d["document"] for d in all_docs])
            max_dist = max(d["distance"] for d in all_docs) if all_docs else 1
            for d in all_docs:
                vec_score = 1 - (d["distance"] / max_dist) if max_dist > 0 else 0
                bm25_score = bm25.score(user_query, all_docs.index(d))
                d["hybrid_score"] = settings.hybrid_search_alpha * bm25_score + (1 - settings.hybrid_search_alpha) * vec_score

            all_docs.sort(key=lambda x: x["hybrid_score"], reverse=True)
            top = all_docs[:settings.retrieval_top_k]

            lines: list[str] = []
            for d in top:
                try:
                    product = json.loads(d["document"])
                    lines.append(self._format_product(product))
                except (json.JSONDecodeError, KeyError):
                    lines.append(d["document"][:200])

            return f"商品检索结果（共 {len(top)} 条）:\n\n" + "\n\n".join(lines) +\
                   "\n\n（以上为结构化商品数据，可直接读取）"

        except Exception as e:
            raise ToolExecutionError(f"商品检索失败: {e}") from e

    @staticmethod
    def _build_where(filters: dict[str, Any]) -> dict[str, Any] | None:
        conditions: list[dict] = []
        if "category" in filters and filters["category"]:
            conditions.append({"category": filters["category"]})
        price_max = filters.get("price_max")
        if price_max is not None:
            try:
                conditions.append({"price": {"$lte": float(price_max)}})
            except (ValueError, TypeError):
                pass
        price_min = filters.get("price_min")
        if price_min is not None:
            try:
                conditions.append({"price": {"$gte": float(price_min)}})
            except (ValueError, TypeError):
                pass
        if len(conditions) == 1:
            return conditions[0]
        if len(conditions) > 1:
            return {"$and": conditions}
        return None
