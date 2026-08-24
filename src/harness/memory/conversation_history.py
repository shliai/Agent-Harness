from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta
from pathlib import Path

from harness.config import settings
from harness.domain.models import AgentMessage

logger = logging.getLogger("harness.memory.conversation_history")

MAX_AGE_HOURS_FALLBACK = 24
LEGACY_DIR = Path("./data/conversations")  # 旧版 JSON 目录（自动迁移源）


class ConversationHistory:
    """会话历史持久化 —— SQLite 版（v0.5.0 起）

    会话状态（messages / summary / working_memory / traces / title）全部落库，
    与业务数据共用 data/harness.db：
    - 原子性与并发由 SQLite 事务保证（替代旧版临时文件 + os.replace）
    - 过期清理基于 updated_at 字段（默认 24h，可配 SESSION_CLEANUP_HOURS）
    - 兼容：首次使用时自动把旧版 data/conversations/*.json 迁移入库

    对外 API 与文件版完全一致（loop / api 无需感知存储变化）。
    `base_path` 参数保留仅为兼容旧签名，不再使用。
    """

    def __init__(self, base_path: Path | None = None) -> None:  # noqa: ARG002
        try:
            self._migrate_legacy_json()
        except Exception as e:
            logger.warning("旧会话 JSON 迁移失败(不影响使用): %s", e)
        self._cleanup_old()

    # ── 旧 JSON 自动迁移 ───────────────────────────────

    def _migrate_legacy_json(self) -> None:
        from harness.storage import db as store

        with store.db() as c:
            n = c.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
        if n > 0 or not LEGACY_DIR.exists():
            return

        migrated = 0
        for f in sorted(LEGACY_DIR.glob("*.json")):
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
                sid = str(data.get("session_id") or f.stem)
                messages = [m for m in data.get("messages", [])]
                self.save_state(
                    sid,
                    [AgentMessage(**m) for m in messages],
                    summary=data.get("summary", ""),
                    working_memory=data.get("working_memory", {}),
                    traces=data.get("traces", []),
                )
                if data.get("title"):
                    self._write_raw_sync(sid, {**data, "title": data["title"]})
                migrated += 1
            except Exception as e:
                logger.warning("跳过无法迁移的会话文件 %s: %s", f.name, e)
        if migrated:
            logger.info("已从 JSON 迁移 %d 个会话到 SQLite", migrated)

    # ── 核心读写（同步实现） ────────────────────────────

    def save_state(
        self,
        session_id: str,
        messages: list[AgentMessage],
        summary: str | None = None,
        working_memory: dict | None = None,
        traces: list[dict] | None = None,
        user_id: str | None = None,
    ) -> Path:
        """保存完整会话状态（整体事务替换 messages，保留既有 title 与归属）

        user_id 为会话归属者；冲突更新时保留首次写入的归属（不可被后续请求篡改）。
        """
        from harness.storage import db as store

        now = datetime.now().isoformat(timespec="seconds")
        with store.db() as c:
            prev = c.execute(
                "SELECT title, created_at, user_id FROM sessions WHERE session_id=?",
                (session_id,),
            ).fetchone()
            title = prev["title"] if prev else ""
            created_at = prev["created_at"] if prev else now
            owner = (prev["user_id"] if prev else "") or (user_id or "")

            c.execute(
                """INSERT INTO sessions(session_id,user_id,title,summary,working_memory_json,
                                        traces_json,updated_at,created_at)
                   VALUES(?,?,?,?,?,?,?,?)
                   ON CONFLICT(session_id) DO UPDATE SET
                     title=excluded.title, summary=excluded.summary,
                     working_memory_json=excluded.working_memory_json,
                     traces_json=excluded.traces_json, updated_at=excluded.updated_at""",
                (session_id, owner, title, summary or "",
                 json.dumps(working_memory or {}, ensure_ascii=False),
                 json.dumps(traces or [], ensure_ascii=False), now, created_at),
            )
            c.execute("DELETE FROM session_messages WHERE session_id=?", (session_id,))
            c.executemany(
                """INSERT INTO session_messages(session_id,seq,role,content,tool_call_id,tool_name,"""
                """ timestamp) VALUES(?,?,?,?,?,?,?)""",
                [
                    (session_id, i, m.role.value, m.content,
                     m.tool_call_id or "", m.tool_name or "", m.timestamp.isoformat())
                    for i, m in enumerate(messages)
                ],
            )
        return Path(str(settings.db_path))

    def load_state(self, session_id: str) -> dict | None:
        from harness.storage import db as store

        with store.db() as c:
            srow = c.execute(
                "SELECT * FROM sessions WHERE session_id=?", (session_id,)
            ).fetchone()
            if not srow:
                return None
            msgs = c.execute(
                "SELECT * FROM session_messages WHERE session_id=? ORDER BY seq",
                (session_id,),
            ).fetchall()

        messages = [
            AgentMessage(
                role=m["role"], content=m["content"],
                tool_call_id=m["tool_call_id"] or None,
                tool_name=m["tool_name"] or None,
            )
            for m in msgs
        ]
        return {
            "session_id": session_id,
            "user_id": srow["user_id"] or "",
            "title": srow["title"] or "",
            "summary": srow["summary"] or "",
            "working_memory": json.loads(srow["working_memory_json"] or "{}"),
            "traces": json.loads(srow["traces_json"] or "[]"),
            "messages": [m.model_dump(mode="json") for m in messages],
            "updated_at": srow["updated_at"],
        }

    def get_owner(self, session_id: str) -> str:
        """仅查归属（不加载消息），供写入前越权校验"""
        from harness.storage import db as store

        with store.db() as c:
            row = c.execute(
                "SELECT user_id FROM sessions WHERE session_id=?", (session_id,)
            ).fetchone()
        return (row["user_id"] if row else "") or ""

    async def aget_owner(self, session_id: str) -> str:
        import asyncio

        return await asyncio.to_thread(self.get_owner, session_id)

    def load(self, session_id: str) -> list[AgentMessage] | None:
        state = self.load_state(session_id)
        if state is None:
            return None
        return [AgentMessage(**m) for m in state["messages"]]

    def save(self, session_id: str, messages: list[AgentMessage]) -> Path:
        return self.save_state(session_id, messages)

    def delete(self, session_id: str) -> None:
        from harness.storage import db as store

        with store.db() as c:
            c.execute("DELETE FROM sessions WHERE session_id=?", (session_id,))
            c.execute("DELETE FROM session_messages WHERE session_id=?", (session_id,))

    def list_sessions(self, user_id: str | None = None) -> list[str]:
        """会话列表；提供 user_id 时仅返回其名下及无归属(遗留)会话"""
        from harness.storage import db as store

        with store.db() as c:
            if user_id:
                rows = c.execute(
                    "SELECT session_id FROM sessions WHERE user_id IN (?, '') "
                    "ORDER BY updated_at DESC",
                    (user_id,),
                ).fetchall()
            else:
                rows = c.execute(
                    "SELECT session_id FROM sessions ORDER BY updated_at DESC"
                ).fetchall()
        return [r["session_id"] for r in rows]

    def _read_raw_sync(self, session_id: str) -> dict | None:
        return self.load_state(session_id)

    def _write_raw_sync(self, session_id: str, data: dict) -> None:
        """重命名等场景：仅更新 title / updated_at"""
        from harness.storage import db as store

        now = datetime.now().isoformat(timespec="seconds")
        with store.db() as c:
            c.execute(
                """INSERT INTO sessions(session_id,user_id,title,summary,working_memory_json,
                                        traces_json,updated_at,created_at)
                   VALUES(?,?,?,?,?,?,?,?)
                   ON CONFLICT(session_id) DO UPDATE SET
                     title=excluded.title, updated_at=excluded.updated_at""",
                (session_id, "", str(data.get("title", "")), str(data.get("summary", "")),
                 "{}", "[]", now, now),
            )

    def _cleanup_old(self) -> None:
        hours = getattr(settings, "session_cleanup_hours", MAX_AGE_HOURS_FALLBACK)
        cutoff = (datetime.now() - timedelta(hours=hours)).isoformat(timespec="seconds")
        from harness.storage import db as store

        with store.db() as c:
            stale = [r["session_id"] for r in c.execute(
                "SELECT session_id FROM sessions WHERE updated_at < ?", (cutoff,)
            ).fetchall()]
            for sid in stale:
                c.execute("DELETE FROM sessions WHERE session_id=?", (sid,))
                c.execute("DELETE FROM session_messages WHERE session_id=?", (sid,))
        if stale:
            logger.info("清理过期会话 %d 个 (>%dh)", len(stale), hours)

    # ── 异步封装 ───────────────────────────────────────

    async def asave_state(
        self,
        session_id: str,
        messages: list[AgentMessage],
        summary: str | None = None,
        working_memory: dict | None = None,
        traces: list[dict] | None = None,
        user_id: str | None = None,
    ) -> Path:
        return await asyncio.to_thread(
            self.save_state, session_id, messages, summary, working_memory, traces, user_id
        )

    async def aload_state(self, session_id: str) -> dict | None:
        return await asyncio.to_thread(self.load_state, session_id)

    async def asave(self, session_id: str, messages: list[AgentMessage]) -> Path:
        return await asyncio.to_thread(self.save, session_id, messages)

    async def aload(self, session_id: str) -> list[AgentMessage] | None:
        return await asyncio.to_thread(self.load, session_id)

    async def adelete(self, session_id: str) -> None:
        await asyncio.to_thread(self.delete, session_id)

    async def alist_sessions(self, user_id: str | None = None) -> list[str]:
        return await asyncio.to_thread(self.list_sessions, user_id)

    async def aread_raw(self, session_id: str) -> dict | None:
        return await asyncio.to_thread(self.load_state, session_id)

    async def awrite_raw(self, session_id: str, data: dict) -> None:
        await asyncio.to_thread(self._write_raw_sync, session_id, data)
