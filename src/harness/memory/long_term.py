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
    """长期记忆：基于 ChromaDB 的向量存储，跨会话语义检索历史对话

    与短期记忆（滑动窗口）和会话历史（JSON 持久化）不同：
    - 短期记忆服务单次 ReAct 循环内的上下文
    - 会话历史按 session_id 隔离，精确恢复
    - 长期记忆跨 session_id，按语义相似度召回，让 Agent "记得"过往交互

    所有写入/检索操作均为 async，内部用 to_thread 把同步的 ChromaDB 调用
    放到线程池执行，避免阻塞 asyncio 事件循环。
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
            return self.client.create_collection(COLLECTION_NAME, embedding_function=ef)
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
        user_id: str = "anonymous",
    ) -> None:
        """异步写入一轮完整对话到长期记忆

        ChromaDB 的 add() 是同步阻塞操作（包含 BGE 编码），放到线程池执行
        以避免阻塞 asyncio 事件循环。
        """
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

    async def search(self, query: str, top_k: int | None = None) -> list[dict[str, Any]]:
        """异步语义检索相关历史对话

        ChromaDB 的 query() 是同步阻塞操作（包含 BGE 编码），放到线程池执行
        以避免阻塞 asyncio 事件循环。
        """
        if not self.enabled or self.collection is None:
            return []
        if not query.strip():
            return []

        try:
            k = top_k or settings.long_term_top_k

            def _do_query():
                count = self.collection.count()
                if count == 0:
                    return []
                n_results = min(k, count)
                return self.collection.query(
                    query_texts=[query],
                    n_results=n_results,
                )

            results = await asyncio.to_thread(_do_query)
            if not results:
                return []

            docs_list = results.get("documents", [[]])[0]
            metas_list = results.get("metadatas", [[]])[0]
            dists_list = results.get("distances", [[]])[0]
            return [
                {
                    "document": doc,
                    "metadata": meta or {},
                    "distance": dist,
                }
                for doc, meta, dist in zip(docs_list, metas_list or [], dists_list or [])
            ]
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
