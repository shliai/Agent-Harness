from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from pathlib import Path
from typing import Any

import chromadb
from chromadb.config import Settings as ChromaSettings

from harness.config import settings
from harness.memory.embeddings import get_embed_fn

logger = logging.getLogger("harness.memory.long_term")

COLLECTION_NAME = "agent_long_term_memory"


class LongTermMemory:
    """长期记忆：基于 ChromaDB 的向量存储，语义检索历史对话

    - 跨 session_id 语义召回，但按 user_id 隔离（多用户互不可见）
    - 检索带距离阈值过滤：不相关的历史不注入 prompt
    - 所有 IO 均为 async（to_thread），不阻塞事件循环
    """

    def __init__(self, store_path: Path | None = None) -> None:
        self.enabled = settings.long_term_enabled
        self.collection: chromadb.Collection | None = None
        if not self.enabled:
            logger.info("LongTermMemory 已禁用 (long_term_enabled=False)")
            return

        try:
            path = str(store_path or settings.long_term_store_path)
            self.client = chromadb.PersistentClient(
                path=path,
                settings=ChromaSettings(anonymized_telemetry=False),
            )
            self._embed_fn = get_embed_fn()
            self.collection = self._get_or_create_collection()
            logger.info("LongTermMemory 初始化完成 (path=%s)", path)
        except Exception as e:
            logger.warning("LongTermMemory 初始化失败，降级关闭: %s", e)
            self.enabled = False
            self.collection = None

    def _get_or_create_collection(self) -> chromadb.Collection | None:
        ef = self._embed_fn or None
        try:
            return self.client.get_collection(COLLECTION_NAME, embedding_function=ef)
        except (ValueError, chromadb.errors.NotFoundError):
            return self.client.create_collection(
                COLLECTION_NAME,
                embedding_function=ef,
                metadata={"hnsw:space": "cosine"},  # 显式余弦空间，距离语义稳定
            )
        except Exception as e:
            logger.warning("无法获取或创建长期记忆集合: %s", e)
            return None

    @staticmethod
    def _format_conversation(user_input: str, assistant_answer: str) -> str:
        """拼接一轮对话作为长期记忆文档"""
        return f"用户: {user_input}\n助手: {assistant_answer}"

    async def add(
        self,
        user_input: str,
        assistant_answer: str,
        session_id: str,
        user_id: str = "default",
    ) -> None:
        """异步写入一轮完整对话到长期记忆"""
        if not self.enabled or self.collection is None:
            return
        if not user_input.strip() or not assistant_answer.strip():
            return

        try:
            doc = self._format_conversation(user_input, assistant_answer)
            ts = datetime.now().isoformat()
            doc_id = f"{session_id}-{ts}"

            await asyncio.to_thread(
                self.collection.add,
                ids=[doc_id],
                documents=[doc],
                metadatas=[{
                    "session_id": session_id,
                    "user_id": user_id,
                    "timestamp": ts,
                    "role": "conversation",
                }],
            )
            logger.debug("长期记忆已写入: %s", doc_id)
        except Exception as e:
            logger.warning("长期记忆写入失败: %s", e)

    async def search(
        self, query: str, top_k: int | None = None, user_id: str | None = None
    ) -> list[dict[str, Any]]:
        """异步语义检索相关历史对话

        - 按 user_id 过滤（多用户隔离）；None 表示不过滤
        - 距离超过 long_term_max_distance 的结果视为不相关，直接丢弃
        """
        if not self.enabled or self.collection is None:
            return []
        if not query.strip():
            return []

        try:
            k = top_k or settings.long_term_top_k
            threshold = settings.long_term_max_distance
            where = {"user_id": user_id} if user_id else None

            def _do_query():
                count = self.collection.count()
                if count == 0:
                    return []
                n_results = min(k, count)
                return self.collection.query(
                    query_texts=[query],
                    n_results=n_results,
                    where=where,
                )

            results = await asyncio.to_thread(_do_query)
            if not results:
                return []

            docs_list = results.get("documents", [[]])[0]
            metas_list = results.get("metadatas", [[]])[0]
            dists_list = results.get("distances", [[]])[0]

            hits: list[dict[str, Any]] = []
            for doc, meta, dist in zip(docs_list, metas_list or [], dists_list or []):
                if threshold is not None and dist is not None and float(dist) > float(threshold):
                    continue  # 不相关的历史不注入
                hits.append({"document": doc, "metadata": meta or {}, "distance": dist})
            return hits
        except Exception as e:
            logger.warning("长期记忆检索失败: %s", e)
            return []

    def count(self) -> int:
        if not self.enabled or self.collection is None:
            return 0
        try:
            return self.collection.count()
        except Exception:
            return 0
