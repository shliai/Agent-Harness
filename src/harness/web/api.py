from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import AsyncGenerator
from datetime import datetime
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from fastapi import Header

from harness.config import settings
from harness.core.agent import Agent
from harness.core.registry import Registry
from harness.guardrails.audit_logger import AuditLogger
from harness.guardrails.base import GuardrailPipeline
from harness.guardrails.injection_guard import InjectionGuard
from harness.guardrails.input_validator import InputValidator
from harness.guardrails.compliance_filter import ComplianceFilter
from harness.guardrails.output_filter import OutputFilter
from harness.guardrails.system_prompt_guard import SystemPromptGuard
from harness.guardrails.rate_limiter import RateLimiter
from harness.observability.logger import set_session_id
from harness.tools.calculator import CalculatorTool
from harness.tools.knowledge_retrieval import KnowledgeRetrievalTool
from harness.tools.logistics_query import LogisticsQueryTool
from harness.tools.aftersale import AfterSaleApplyTool, AfterSaleQueryTool
from harness.tools.order_query import MyOrdersTool, OrderQueryTool
from harness.tools.policy_query import PolicyQueryTool, TransferHumanTool

logger = logging.getLogger("harness.web.api")

# session_id 白名单：只允许安全字符，杜绝路径穿越（..\ / %2e%2e%2f 等）
SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

_agent: Agent | None = None
def _new_session_id() -> str:
    """会话 ID：uuid4 保证唯一，输出满足白名单 ^[A-Za-z0-9_-]{1,64}$"""
    import uuid

    return uuid.uuid4().hex[:12]
# 同一会话串行化：原子写保证文件不损坏，但并发读改写仍会互相覆盖，这里加锁。
# 语义统一为「忙即拒绝」：locked 检查与获取之间没有让渡点（无竞争的 acquire
# 同步完成），要么拿到锁、要么返回 None 由调用方回 429（绝不排队等待）。
_session_locks: dict[str, asyncio.Lock] = {}


async def _claim_session_lock(session_id: str) -> asyncio.Lock | None:
    """尝试占用会话锁：会话正被处理时返回 None（调用方应拒绝请求）"""
    lock = _session_locks.setdefault(session_id, asyncio.Lock())
    if lock.locked():
        return None
    # 无竞争路径同步完成、不让出事件循环，锁定状态对后续请求立即可见
    await lock.acquire()
    return lock


def _release_session_lock(session_id: str, lock: asyncio.Lock) -> None:
    """释放并清理空闲锁，防止 _session_locks 随会话数无限增长。

    仅当锁未被持有、且字典仍指向同一对象时移除；若新请求刚经 setdefault
    拿到同一把锁并已锁定，则跳过清理。"""
    if lock.locked():
        lock.release()
    if not lock.locked() and _session_locks.get(session_id) is lock:
        _session_locks.pop(session_id, None)

# 管理接口独立限流：每 Token 每分钟 30 次（进程内；多实例换 Redis）
_admin_limiter = RateLimiter(max_requests=30, window_seconds=60)


def _ensure_session_owner(session_id: str, x_user_id: str | None) -> None:
    """会话归属校验：请求携带 X-User-Id 且与会话归属不一致时拒绝（403）

    归属为空的历史遗留/匿名会话不限制，保持单用户模式向后兼容。
    """
    state = get_agent().conversation_history.load_state(session_id)
    if state is None:
        raise HTTPException(status_code=404, detail="Session not found")
    owner = state.get("user_id") or ""
    if x_user_id and owner and x_user_id != owner:
        raise HTTPException(status_code=403, detail="无权访问该会话")


def validate_session_id(session_id: str) -> str:
    if not SESSION_ID_RE.fullmatch(session_id):
        raise HTTPException(status_code=400, detail="非法的 session_id")
    return session_id


def _build_agent() -> Agent:
    """构造一个完整的 Agent 实例（注册工具 + 装配护栏）。"""
    registry = Registry()
    registry.register_tool(KnowledgeRetrievalTool())
    registry.register_tool(CalculatorTool())
    registry.register_tool(OrderQueryTool())
    registry.register_tool(LogisticsQueryTool())
    registry.register_tool(PolicyQueryTool())
    registry.register_tool(TransferHumanTool())
    registry.register_tool(MyOrdersTool())
    registry.register_tool(AfterSaleApplyTool())
    registry.register_tool(AfterSaleQueryTool())

    guardrails = GuardrailPipeline()
    guardrails.add(InputValidator(max_length=4096))
    guardrails.add(InjectionGuard())
    guardrails.add(OutputFilter())
    guardrails.add(ComplianceFilter())
    guardrails.add(SystemPromptGuard())  # 输出层拦截系统提示词泄露（确定性兜底）
    guardrails.add(RateLimiter(
        max_requests=settings.rate_limit_max_requests,
        window_seconds=settings.rate_limit_window_seconds,
    ))
    guardrails.add(AuditLogger())  # 放最后：拦截事件由流水线回调补记

    return Agent(registry=registry, guardrails=guardrails)


def get_agent() -> Agent:
    global _agent
    if _agent is None:
        _agent = _build_agent()
    return _agent


def warmup_agent() -> None:
    """服务启动时主动初始化 Agent，把工具注册与学习记忆装配提前到启动阶段。"""
    global _agent
    if _agent is None:
        _agent = _build_agent()
        logger.info(
            "Agent 预热完成 | tools=%d guardrails=%d",
            len(_agent.registry.list_tools()),
            len(_agent.guardrails.pipes),
        )


# ── 请求/响应模型 ─────────────────────────────────────

class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=4096)
    session_id: str | None = None
    user_id: str | None = Field(default=None, max_length=64)
    stream: bool = True


class ChatResponse(BaseModel):
    answer: str
    session_id: str
    steps: list[dict[str, Any]] = []
    total_duration_ms: float = 0.0
    total_tokens: int = 0
    success: bool = True
    error: str | None = None


class RenameRequest(BaseModel):
    title: str = Field(min_length=1, max_length=100)


class BatchDeleteSessions(BaseModel):
    ids: list[str] = Field(default_factory=list, max_length=2000)


class ClearSessionRequest(BaseModel):
    session_id: str = Field(min_length=1, max_length=64)


class AfterSaleReview(BaseModel):
    note: str = Field(default="", max_length=200)


class ProductIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    category: str = Field(min_length=1, max_length=40)
    brand: str = Field(default="", max_length=60)
    price: float = Field(ge=0)
    description: str = Field(default="", max_length=1000)
    specs: dict[str, str] = Field(default_factory=dict)
    tags: list[str] = Field(default_factory=list)
    status: str = Field(default="在售", pattern="^(在售|下架)$")
    stock: int = Field(default=100, ge=0, le=999999)


# ── 商品管理（生产化：SQLite 为事实源，向量库联动同步）────

def require_admin(x_admin_token: str | None) -> None:
    from harness.domain.exceptions import RateLimitError

    if not settings.admin_token or x_admin_token != settings.admin_token:
        raise HTTPException(status_code=403, detail="管理员鉴权失败")
    try:
        _admin_limiter.check(
            {"type": "input", "content": "admin", "session_id": f"adm:{x_admin_token}"}
        )
    except RateLimitError as e:
        raise HTTPException(status_code=429, detail=str(e))


def _product_in_to_row(payload: "ProductIn", pid: str) -> dict:
    from datetime import datetime

    return {
        "id": pid,
        "name": payload.name,
        "stock": payload.stock,
        "category": payload.category,
        "brand": payload.brand,
        "price": payload.price,
        "description": payload.description,
        "specs": payload.specs,
        "tags": payload.tags,
        "status": payload.status,
        "updated_at": datetime.now().isoformat(timespec="seconds"),
    }


def _db_upsert_row(row: dict) -> None:
    from harness.storage import db as store

    with store.db() as c:
        store.upsert_product(c, row)


async def _sync_upsert(row: dict) -> None:
    from harness.storage import vector_sync

    await asyncio.to_thread(vector_sync.upsert_products, [row])


# ── FastAPI 应用 ──────────────────────────────────────

def create_app() -> FastAPI:
    from pathlib import Path

    app = FastAPI(
        title="Agent Harness API",
        version="0.8.0",
        description="智能体运行时外壳 — Web API",
    )

    if settings.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_origins,
            allow_credentials=False,
            allow_methods=["*"],
            allow_headers=["*"],
        )

    static_dir = Path(__file__).parent / "static"
    if static_dir.exists():
        app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    if not settings.admin_token:
        logger.warning(
            "ADMIN_TOKEN 未配置：商品/售后管理端 API 已全部禁用（fail-closed）。"
            "需要使用请在 .env 中显式设置 ADMIN_TOKEN。"
        )

    # 启动时主动初始化 Agent，避免首次请求等待工具注册和 ChromaDB 初始化
    warmup_agent()

    @app.get("/")
    async def index():
        index_path = static_dir / "index.html"
        if index_path.exists():
            return HTMLResponse(
                await asyncio.to_thread(index_path.read_text, encoding="utf-8"),
                headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
            )
        return HTMLResponse("<h1>Agent Harness</h1><p>Frontend not found</p>")

    @app.get("/health")
    async def health():
        agent = get_agent()
        kb_tool = agent.registry.get_tool("knowledge_retrieval")
        kb_count = 0
        if hasattr(kb_tool, "collection") and kb_tool.collection is not None:
            try:
                kb_count = kb_tool.collection.count()
            except Exception:
                pass
        return {
            "status": "ok",
            "version": "0.8.0",
            "model": settings.openai_model,
            "components": {
                "knowledge_base_documents": kb_count,
                "learning_enabled": bool(
                    agent.learning_store and agent.learning_store.enabled
                ),
                "learning_records": agent.learning_store.count(),
                "tools": len(agent.registry.list_tools()),
            },
        }

    @app.get("/api/tools")
    async def list_tools():
        agent = get_agent()
        result = []
        for name in agent.registry.list_tools():
            tool = agent.registry.get_tool(name)
            result.append({
                "name": name,
                "description": tool.spec.description,
                "parameters": tool.spec.parameters,
            })
        return {"tools": result}

    @app.get("/api/metrics")
    async def get_metrics():
        """进程级聚合指标 + 最近追踪记录"""
        agent = get_agent()
        return {
            "metrics": agent.metrics.snapshot(),
            "recent_traces": agent.tracer.get_log()[-50:],
        }

    @app.post("/api/admin/products")
    async def admin_create_product(payload: ProductIn, x_admin_token: str | None = Header(default=None)):
        """新增商品：写 SQLite + 向量库 upsert（BGE 编码放线程池）"""
        require_admin(x_admin_token)
        import uuid

        from harness.storage import db as store
        from harness.storage import vector_sync

        pid = f"product_{uuid.uuid4().hex[:10]}"
        row = _product_in_to_row(payload, pid)
        await asyncio.to_thread(_db_upsert_row, row)
        await _sync_upsert(row)
        return {"status": "created", "id": pid}

    @app.put("/api/admin/products/{pid}")
    async def admin_update_product(
        pid: str, payload: ProductIn, x_admin_token: str | None = Header(default=None)
    ):
        require_admin(x_admin_token)
        from harness.storage import db as store

        old = await asyncio.to_thread(store.get_product, pid)
        if not old:
            raise HTTPException(status_code=404, detail="商品不存在")

        row = _product_in_to_row(payload, pid)
        await asyncio.to_thread(_db_upsert_row, row)
        await _sync_upsert(row)  # upsert 幂等，价格/描述变化都会刷新索引与 metadata
        return {"status": "updated", "id": pid}

    @app.delete("/api/admin/products/{pid}")
    async def admin_delete_product(pid: str, x_admin_token: str | None = Header(default=None)):
        """下架并移除：DB 删除 + 向量索引同步 delete（修复下架残留）"""
        require_admin(x_admin_token)
        from harness.storage import db as store
        from harness.storage import vector_sync

        ok = await asyncio.to_thread(store.delete_product, pid)
        if not ok:
            raise HTTPException(status_code=404, detail="商品不存在")
        await asyncio.to_thread(vector_sync.delete_product, pid)
        return {"status": "deleted", "id": pid}

    @app.get("/api/admin/products")
    async def admin_list_products(status: str | None = None, x_admin_token: str | None = Header(default=None)):
        require_admin(x_admin_token)
        from harness.storage import db as store

        rows = await asyncio.to_thread(store.list_products, status)
        return {"products": rows, "total": len(rows)}

    @app.post("/api/admin/products/reindex")
    async def admin_reindex(x_admin_token: str | None = Header(default=None)):
        """对账式全量重建向量库：DB 为事实源，prune 清理脏 id"""
        require_admin(x_admin_token)
        from harness.storage import vector_sync

        result = await asyncio.to_thread(vector_sync.reindex_all, True)
        return {"status": "reindexed", **result}

    # ── 售后审核（商家侧） ──
    @app.get("/api/admin/aftersales")
    async def admin_aftersales(status: str | None = None, x_admin_token: str | None = Header(default=None)):
        require_admin(x_admin_token)
        from harness.tools.aftersale_admin import list_for_admin

        rows = await asyncio.to_thread(list_for_admin, status)
        return {"aftersales": rows, "total": len(rows)}

    @app.post("/api/admin/aftersales/{as_id}/approve")
    async def admin_approve_aftersale(
        as_id: str, req: AfterSaleReview, x_admin_token: str | None = Header(default=None)
    ):
        require_admin(x_admin_token)
        from harness.tools.aftersale_admin import AfterSaleNotFound, approve

        try:
            row = await asyncio.to_thread(approve, as_id, "admin", req.note)
        except AfterSaleNotFound:
            raise HTTPException(status_code=404, detail="售后单不存在")
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        return {"status": row["status"], "as_id": as_id}

    @app.post("/api/admin/aftersales/{as_id}/reject")
    async def admin_reject_aftersale(
        as_id: str, req: AfterSaleReview, x_admin_token: str | None = Header(default=None)
    ):
        require_admin(x_admin_token)
        from harness.tools.aftersale_admin import AfterSaleNotFound, reject

        try:
            row = await asyncio.to_thread(reject, as_id, req.note, "admin")
        except AfterSaleNotFound:
            raise HTTPException(status_code=404, detail="售后单不存在")
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        return {"status": row["status"], "as_id": as_id}

    @app.post("/api/admin/aftersales/{as_id}/complete")
    async def admin_complete_aftersale(as_id: str, x_admin_token: str | None = Header(default=None)):
        """打款完成（模拟）：已通过 → 已完成"""
        require_admin(x_admin_token)
        from harness.tools.aftersale_admin import AfterSaleNotFound, complete

        try:
            row = await asyncio.to_thread(complete, as_id, "admin")
        except AfterSaleNotFound:
            raise HTTPException(status_code=404, detail="售后单不存在")
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        return {"status": row["status"], "as_id": as_id, "refund_amount": row.get("refund_amount", 0)}

    @app.post("/api/chat")
    async def chat(req: ChatRequest):
        session_id = req.session_id or _new_session_id()
        validate_session_id(session_id)
        agent = get_agent()

        # 跨用户写保护：已归属会话拒绝其他用户追加内容
        existing_owner = await agent.conversation_history.aget_owner(session_id)
        if req.user_id and existing_owner and req.user_id != existing_owner:
            raise HTTPException(status_code=403, detail="无权向该会话写入")

        if req.stream:
            return StreamingResponse(
                _stream_chat(agent, req.message, session_id, user_id=req.user_id),
                media_type="text/event-stream",
                headers={
                    "Cache-Control": "no-cache",
                    "Connection": "keep-alive",
                    "X-Accel-Buffering": "no",
                },
            )

        # 非流式：忙即 429（与流式路径语义一致），锁用完即清理
        lock = await _claim_session_lock(session_id)
        if lock is None:
            raise HTTPException(status_code=429, detail="当前会话有请求正在处理，请稍候")
        try:
            result = await agent.run(req.message, session_id=session_id, user_id=req.user_id)
        finally:
            _release_session_lock(session_id, lock)
        return ChatResponse(
            answer=result.answer,
            session_id=session_id,
            steps=[s.model_dump(mode="json") for s in result.steps],
            total_duration_ms=result.total_duration_ms,
            total_tokens=result.total_tokens,
            success=result.success,
            error=result.error,
        )

    @app.post("/api/session/clear")
    async def clear_session(req: ClearSessionRequest, x_user_id: str | None = Header(default=None)):
        session_id = validate_session_id(req.session_id)
        _ensure_session_owner(session_id, x_user_id)
        agent = get_agent()
        await agent.conversation_history.adelete(session_id)
        return {"status": "cleared", "session_id": session_id}

    @app.get("/api/sessions")
    async def list_sessions(x_user_id: str | None = Header(default=None)):
        agent = get_agent()
        sessions = await agent.conversation_history.alist_sessions(x_user_id)
        result: list[dict[str, Any]] = []

        async def _load_one(sid: str) -> dict[str, Any]:
            data = await agent.conversation_history.aread_raw(sid)
            if not data:
                return {"id": sid, "title": sid, "updated_at": "", "message_count": 0}
            msgs = data.get("messages", [])
            first_user = ""
            for m in msgs:
                if m.get("role") == "user":
                    first_user = str(m.get("content", ""))[:50]
                    break
            return {
                "id": sid,
                "title": data.get("title") or first_user or "(空会话)",
                "updated_at": data.get("updated_at", ""),
                "message_count": len(msgs),
            }

        results = await asyncio.gather(*[_load_one(s) for s in sessions])
        result = list(results)
        result.sort(key=lambda x: x["updated_at"], reverse=True)
        return {"sessions": result}

    @app.delete("/api/sessions/{session_id}")
    async def delete_session(session_id: str, x_user_id: str | None = Header(default=None)):
        validate_session_id(session_id)
        _ensure_session_owner(session_id, x_user_id)
        agent = get_agent()
        await agent.conversation_history.adelete(session_id)
        return {"status": "deleted"}

    @app.post("/api/sessions/batch-delete")
    async def batch_delete_sessions(req: BatchDeleteSessions,
                                    x_user_id: str | None = Header(default=None)):
        """批量删除会话：仅允许删除本人名下或无归属(遗留)的会话"""
        import asyncio as _aio

        agent = get_agent()
        deleted, skipped = [], []
        for sid in dict.fromkeys(req.ids):
            try:
                validate_session_id(sid)
            except HTTPException:
                skipped.append(sid)
                continue
            data = await _aio.to_thread(store_read := agent.conversation_history.load_state, sid)
            if data is None:
                skipped.append(sid)
                continue
            owner = data.get("user_id") or ""
            if x_user_id and owner and x_user_id != owner:
                skipped.append(sid)  # 越权静默跳过，不暴露存在性
                continue
            await agent.conversation_history.adelete(sid)
            deleted.append(sid)
        return {"status": "ok", "deleted": deleted, "skipped": len(skipped)}

    @app.put("/api/sessions/{session_id}")
    async def rename_session(
        session_id: str, req: RenameRequest, x_user_id: str | None = Header(default=None)
    ):
        validate_session_id(session_id)
        _ensure_session_owner(session_id, x_user_id)
        agent = get_agent()
        data = await agent.conversation_history.aread_raw(session_id)
        if data is None:
            raise HTTPException(status_code=404, detail="Session not found")
        data["title"] = req.title
        data["updated_at"] = datetime.now().isoformat()
        await agent.conversation_history.awrite_raw(session_id, data)
        return {"status": "renamed", "title": req.title}

    @app.get("/api/sessions/{session_id}")
    async def get_session(session_id: str, x_user_id: str | None = Header(default=None)):
        validate_session_id(session_id)
        agent = get_agent()
        data = await agent.conversation_history.aread_raw(session_id)
        if data is None:
            raise HTTPException(status_code=404, detail="Session not found")
        owner = data.get("user_id") or ""
        if x_user_id and owner and x_user_id != owner:
            raise HTTPException(status_code=403, detail="无权访问该会话")
        # 只暴露客户端需要的字段；chapters/summary 属提示词内部态，不外泄
        return {
            "session_id": session_id,
            "title": data.get("title") or "",
            "user_id": owner,
            "messages": data.get("messages") or [],
            "working_memory": data.get("working_memory") or {},
            "traces": data.get("traces") or [],
            "updated_at": data.get("updated_at"),
        }

    return app


async def _stream_chat(
    agent: Agent, message: str, session_id: str, user_id: str | None = None
) -> AsyncGenerator[str, None]:
    """SSE 真流式响应：每个 ReAct 步骤即时推送（同会话加锁串行）

    同时把 session_id 注入日志上下文，本轮内所有日志（含后台记忆整理任务）
    自动携带 session_id；生成器关闭/退出时统一清理。
    """
    set_session_id(session_id)
    lock: asyncio.Lock | None = None
    try:
        yield f"data: {json.dumps({'type': 'meta', 'session_id': session_id}, ensure_ascii=False)}\n\n"

        # 忙即拒绝（与非流式路径语义一致），锁用完即清理
        lock = await _claim_session_lock(session_id)
        if lock is None:
            yield f"data: {json.dumps({'type': 'error', 'message': '当前会话有请求正在处理，请稍候'}, ensure_ascii=False)}\n\n"
            yield "data: [DONE]\n\n"
            return

        try:
            async for event in agent.loop.execute_stream(
                message, session_id=session_id, user_id=user_id
            ):
                etype = event.get("type")
                if etype == "step":
                    payload: dict[str, Any] = {
                        "type": "step",
                        "step_index": event.get("step_index"),
                    }
                    if event.get("thought"):
                        payload["thought"] = event["thought"]
                    if event.get("tool_call"):
                        payload["tool_call"] = event["tool_call"]
                    if event.get("tool_result"):
                        payload["tool_result"] = event["tool_result"]
                    if event.get("final"):
                        payload["final"] = True
                    yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
                elif etype == "delta":
                    yield f"data: {json.dumps({'type': 'delta', 'content': event.get('content', '')}, ensure_ascii=False)}\n\n"
                elif etype == "delta_reset":
                    reason = event.get("reason", "")
                    yield ('data: {"type": "delta_reset", "reason": "%s"}\n\n' % reason) if reason else 'data: {"type": "delta_reset"}\n\n'
                elif etype == "answer_replace":
                    yield f"data: {json.dumps({'type': 'answer_replace', 'content': event.get('content', '')}, ensure_ascii=False)}\n\n"
                elif etype == "result":
                    yield f"data: {json.dumps({'type': 'result', 'answer': event['answer'], 'total_duration_ms': event['total_duration_ms'], 'total_steps': event['total_steps'], 'total_tokens': event.get('total_tokens', 0), 'success': event['success']}, ensure_ascii=False)}\n\n"
                elif etype == "error":
                    err_result = event.get("result")
                    answer = err_result.answer if err_result else event.get("message", "")
                    yield f"data: {json.dumps({'type': 'error', 'message': answer}, ensure_ascii=False)}\n\n"
        except Exception as e:
            logger.exception("流式响应异常")
            yield f"data: {json.dumps({'type': 'error', 'message': str(e)}, ensure_ascii=False)}\n\n"

        yield "data: [DONE]\n\n"
    finally:
        if lock is not None:
            _release_session_lock(session_id, lock)
        set_session_id(None)


def run_server(host: str = "localhost", port: int = 8000) -> None:
    import uvicorn

    app = create_app()
    logger.info("启动 Web 服务器: http://%s:%d", host, port)
    uvicorn.run(app, host=host, port=port, log_level="info")
