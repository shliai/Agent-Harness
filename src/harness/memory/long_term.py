from __future__ import annotations

import asyncio
import logging
import re
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
        document: str | None = None,
    ) -> None:
        """异步写入一轮记忆（优先使用整理后的结构化事实文档）

        文档按「事实行」拆分为多条记录（每条带 entity_key/relation 元数据），
        支持写时替换：同会话同实体同可变关系的旧记录被删除，只留最新值。
        """
        if not self.enabled or self.collection is None:
            return
        if not document and (not user_input.strip() or not assistant_answer.strip()):
            return

        try:
            ts = datetime.now().isoformat(timespec="seconds")

            if document:
                # 事实级写入：一行一记录，元数据携带实体键用于更新语义
                lines = [ln.strip() for ln in document.splitlines() if ln.strip()]
                entries = []
                for idx, line in enumerate(lines):
                    toks = line.split(" ")
                    subject = toks[0] if toks else ""
                    relation = toks[1] if len(toks) > 1 else ""
                    entries.append({
                        "id": f"{session_id}-{ts}-{idx}",
                        "doc": line[:200],
                        "entity_key": subject,
                        "relation": relation,
                    })
                # 写时替换：同会话内同实体+可变关系的旧记录删除
                mutable = {"状态", "进度", "预计", "地址"}
                for e in entries:
                    if e["relation"] in mutable and e["entity_key"]:
                        stale = await asyncio.to_thread(
                            self.collection.get,
                            where={"$and": [
                                {"user_id": user_id},
                                {"session_id": session_id},
                                {"entity_key": e["entity_key"]},
                                {"relation": e["relation"]},
                            ]},
                            include=["metadatas"],
                        )
                        old_ids = [i for i in (stale.get("ids") or [])]
                        if old_ids:
                            await asyncio.to_thread(self.collection.delete, ids=old_ids)
                await asyncio.to_thread(
                    self.collection.add,
                    ids=[e["id"] for e in entries],
                    documents=[e["doc"] for e in entries],
                    metadatas=[{
                        "session_id": session_id,
                        "user_id": user_id,
                        "timestamp": ts,
                        "role": "facts",
                        "entity_key": e["entity_key"],
                        "relation": e["relation"],
                    } for e in entries],
                )
                logger.debug("长期记忆已写入 %d 条事实 (%s)", len(entries), session_id)
            else:
                doc = self._format_conversation(user_input, assistant_answer)
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
        self,
        query: str,
        top_k: int | None = None,
        user_id: str | None = None,
        exclude_session_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """异步语义检索相关历史对话

        - 按 user_id 过滤（多用户隔离）；None 表示不过滤
        - exclude_session_id：排除当前会话——本会话近期内容已在短期窗口，
          召回只补「跨会话」的远期记忆，避免重复注入
        - 距离超过 long_term_max_distance 的结果视为不相关，直接丢弃
        """
        if not self.enabled or self.collection is None:
            return []
        if not query.strip():
            return []

        try:
            k = top_k or settings.long_term_top_k
            threshold = settings.long_term_max_distance

            where: dict[str, Any] | None = None
            if user_id and exclude_session_id:
                where = {"$and": [
                    {"user_id": user_id},
                    {"session_id": {"$ne": exclude_session_id}},
                ]}
            elif user_id:
                where = {"user_id": user_id}

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

            # 读时保鲜：可变关系（状态/进度等）同实体多命中时只保留时间戳最新一条，
            # 抑制跨会话的过期状态误导；其余关系全部保留
            mutable = {"状态", "进度", "预计", "地址"}
            newest: dict[tuple[str, str], dict[str, Any]] = {}
            keep: list[dict[str, Any]] = []
            for h in hits:
                meta = h.get("metadata") or {}
                ek, rel = str(meta.get("entity_key") or ""), str(meta.get("relation") or "")
                if rel in mutable and ek:
                    key = (ek, rel)
                    cur = newest.get(key)
                    if cur is None or str(meta.get("timestamp") or "") > str(
                        (cur.get("metadata") or {}).get("timestamp") or ""
                    ):
                        newest[key] = h
                else:
                    keep.append(h)
            hits = sorted(
                keep + list(newest.values()),
                key=lambda h: str((h.get("metadata") or {}).get("timestamp") or ""),
            )
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

    # ── 维护：TTL / 近重复合并 / 孤儿清除 / 容量熔断 ──────

    _HIGH_VALUE_RE = re.compile(
        r"20\d{9,13}|\b(SF|YT|ZTO|STO|JD|EMS)\d{9,12}\b|承诺|\d{3,}元", re.IGNORECASE
    )

    @classmethod
    def _value_score(cls, doc: str) -> int:
        """价值分：含订单号/物流号/金额/承诺等硬信息为高价值（TTL 豁免）"""
        score = 0
        if cls._HIGH_VALUE_RE.search(doc):
            score += 2
        if len(doc) >= 60:
            score += 1
        return score

    async def maintain(self, live_session_ids: set[str] | None = None) -> dict:
        """启动维护（后台执行一次）：清理 + 合并 + 熔断，返回统计

        - TTL：超过 ttl_days 的低价值记录删除；高价值豁免
        - 近重复：与更新记录距离 < dup_distance 的旧条合并删除
        - 孤儿：session 已不存在 → 删除（需传 live_session_ids）
        - 容量：超 max_records 时按「低价值且最旧」优先淘汰
        """
        stats = {"ttl_deleted": 0, "dup_merged": 0, "orphan_deleted": 0, "cap_evicted": 0}
        if not self.enabled or self.collection is None:
            return stats
        try:
            got = await asyncio.to_thread(
                self.collection.get, include=["documents", "metadatas"]
            )
            ids = list(got.get("ids") or [])
            docs = list(got.get("documents") or [])
            metas = list(got.get("metadatas") or [])
            now = datetime.now()

            def age_days(m: dict) -> float:
                ts = str(m.get("timestamp") or "")
                try:
                    return (now - datetime.fromisoformat(ts)).days
                except Exception:
                    return 999.0

            scores = {i: self._value_score(d or "") for i, d in zip(ids, docs)}
            delete: set[str] = set()

            # ① TTL（高价值豁免）
            ttl = settings.long_term_ttl_days
            for i, m in zip(ids, metas):
                if age_days(m or {}) > ttl and scores[i] < 2:
                    delete.add(i)

            # ③ 孤儿清除
            if live_session_ids is not None:
                for i, m in zip(ids, metas):
                    sid = str((m or {}).get("session_id") or "")
                    if sid and sid not in live_session_ids:
                        delete.add(i)

            # ② 近重复合并：旧条若与某条「更新记录」几乎相同则删旧保新
            if settings.long_term_dup_distance is not None and len(ids) > 1:
                order = sorted(range(len(ids)), key=lambda k: age_days(metas[k] or {}))
                for pos, k in enumerate(order):
                    iid = ids[k]
                    if iid in delete:
                        continue
                    nearest = await asyncio.to_thread(
                        self.collection.query,
                        query_texts=[docs[k]],
                        n_results=min(2, len(ids)),
                    )
                    near_ids = (nearest.get("ids") or [[]])[0]
                    near_dists = (nearest.get("distances") or [[]])[0]
                    for nid, dist in zip(near_ids[1:], near_dists[1:]):
                        if nid in delete:
                            continue
                        other_pos = next(
                            (p for p, j in enumerate(order) if ids[j] == nid), None
                        )
                        # 只向「更新」的记录合并
                        if other_pos is not None and other_pos < pos \
                                and float(dist) < settings.long_term_dup_distance:
                            delete.add(iid)
                            stats["dup_merged"] += 1
                            break

            # 执行删除（TTL/孤儿/去重）
            if delete:
                await asyncio.to_thread(self.collection.delete, ids=list(delete))
                stats["ttl_deleted"] = sum(
                    1 for i in delete
                    if age_days(dict(zip(ids, metas))[i] or {}) > ttl and scores[i] < 2
                )
                stats["orphan_deleted"] = len(delete) - stats["ttl_deleted"] - stats.get("dup_merged", 0)

            # ④ 容量熔断
            overflow = self.count() - settings.long_term_max_records
            if overflow > 0:
                got2 = await asyncio.to_thread(
                    self.collection.get, include=["documents", "metadatas"]
                )
                cand = sorted(
                    zip(got2["ids"], got2.get("documents") or [], got2.get("metas") or got2.get("metadatas") or []),
                    key=lambda t: (self._value_score(t[1] or ""), str((t[2] or {}).get("timestamp") or "")),
                )
                evict = [t[0] for t in cand[:overflow]]
                if evict:
                    await asyncio.to_thread(self.collection.delete, ids=evict)
                    stats["cap_evicted"] = len(evict)

            if any(stats.values()):
                logger.info("长期记忆维护完成: %s", stats)
            return stats
        except Exception as e:
            logger.warning("长期记忆维护失败(不影响使用): %s", e)
            return stats
