from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncGenerator
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from harness.config import settings
from harness.core.agent import Agent
from harness.core.registry import Registry
from harness.domain.models import AgentResult
from harness.guardrails.base import GuardrailPipeline
from harness.guardrails.audit_logger import AuditLogger
from harness.guardrails.input_validator import InputValidator
from harness.guardrails.output_filter import OutputFilter
from harness.guardrails.rate_limiter import RateLimiter
from harness.memory.session import SessionManager
from harness.tools.calculator import CalculatorTool
from harness.tools.knowledge_retrieval import KnowledgeRetrievalTool
from harness.tools.logistics_query import LogisticsQueryTool
from harness.tools.order_query import OrderQueryTool

logger = logging.getLogger("harness.web.api")

# ── 全局单例 ──────────────────────────────────────────
_agent: Agent | None = None
_sessions: dict[str, Agent] = {}
_session_mgr = SessionManager()


def _build_agent() -> Agent:
    """构造一个完整的 Agent 实例（注册工具 + 装配护栏）。"""
    registry = Registry()
    registry.register_tool(KnowledgeRetrievalTool())
    registry.register_tool(CalculatorTool())
    registry.register_tool(OrderQueryTool())
    registry.register_tool(LogisticsQueryTool())

    guardrails = GuardrailPipeline()
    guardrails.add(InputValidator(max_length=4096))
    guardrails.add(OutputFilter())
    guardrails.add(RateLimiter(
        max_requests=settings.rate_limit_max_requests,
        window_seconds=settings.rate_limit_window_seconds,
    ))
    guardrails.add(AuditLogger())

    return Agent(registry=registry, guardrails=guardrails)


def get_agent(session_id: str | None = None) -> Agent:
    global _agent
    if session_id and session_id in _sessions:
        return _sessions[session_id]

    if _agent is None:
        _agent = _build_agent()

    if session_id:
        _sessions[session_id] = _agent
    return _agent


def warmup_agent() -> None:
    """服务启动时主动初始化 Agent，把工具注册和 ChromaDB 初始化提前到启动阶段。

    这样首次请求不再需要等待这些初始化，直接进入 LLM 调用。
    """
    global _agent
    if _agent is None:
        _agent = _build_agent()
        logger.info("Agent 预热完成 | tools=%d guardrails=%d",
                    len(_agent.registry.list_tools()),
                    len(_agent.guardrails.pipes))


# ── 请求/响应模型 ─────────────────────────────────────

class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=4096)
    session_id: str | None = None
    stream: bool = True


class ChatResponse(BaseModel):
    answer: str
    session_id: str
    steps: list[dict[str, Any]] = []
    total_duration_ms: float = 0.0
    success: bool = True
    error: str | None = None

class RenameRequest(BaseModel):
    title: str = Field(min_length=1, max_length=100)


class ClearSessionRequest(BaseModel):
    session_id: str = Field(min_length=1)


# ── FastAPI 应用 ──────────────────────────────────────

def create_app() -> FastAPI:
    import os
    from pathlib import Path

    app = FastAPI(
        title="Agent Harness API",
        version="0.1.0",
        description="智能体运行时外壳 — Web API",
    )

    static_dir = Path(__file__).parent / "static"
    if static_dir.exists():
        app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    # 启动时主动初始化 Agent，避免首次请求等待工具注册和 ChromaDB 初始化
    warmup_agent()

    @app.get("/")
    async def index():
        index_path = static_dir / "index.html"
        if index_path.exists():
            # no-cache: 浏览器每次都发请求验证，确保拿到最新版本
            # 避免静态 HTML 被缓存导致前端改动不生效
            return HTMLResponse(
                index_path.read_text(encoding="utf-8"),
                headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
            )
        return HTMLResponse("<h1>Agent Harness</h1><p>Frontend not found</p>")

    @app.get("/health")
    async def health():
        return {"status": "ok", "version": "0.1.0"}

    @app.get("/api/tools")
    async def list_tools():
        agent = get_agent()
        tools = agent.registry.list_tools()
        result = []
        for name in tools:
            tool = agent.registry.get_tool(name)
            result.append({
                "name": name,
                "description": tool.spec.description,
                "parameters": tool.spec.parameters,
            })
        return {"tools": result}

    @app.post("/api/chat")
    async def chat(req: ChatRequest, request: Request):
        session_id = req.session_id or _session_mgr.generate_session_id()
        agent = get_agent(session_id)

        if req.stream:
            return StreamingResponse(
                _stream_chat(agent, req.message, session_id),
                media_type="text/event-stream",
                headers={
                    "Cache-Control": "no-cache",
                    "Connection": "keep-alive",
                    "X-Accel-Buffering": "no",
                },
            )

        result = await agent.run(req.message, session_id=session_id)
        return ChatResponse(
            answer=result.answer,
            session_id=session_id,
            steps=[s.model_dump() for s in result.steps],
            total_duration_ms=result.total_duration_ms,
            success=result.success,
            error=result.error,
        )

    @app.post("/api/session/clear")
    async def clear_session(req: ClearSessionRequest):
        session_id = req.session_id
        agent = get_agent()
        agent.conversation_history.delete(session_id)
        if session_id in _sessions:
            del _sessions[session_id]
        return {"status": "cleared", "session_id": session_id}

    @app.get("/api/sessions")
    async def list_sessions():
        agent = get_agent()
        sessions = agent.conversation_history.list_sessions()
        result = []
        for sid in sessions:
            path = agent.conversation_history.base_path / f"{sid}.json"
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                msgs = data.get("messages", [])
                first_user = ""
                for m in msgs:
                    if m.get("role") == "user":
                        first_user = m["content"][:50]
                        break
                result.append({
                    "id": sid,
                    "title": data.get("title") or first_user or "(空会话)",
                    "updated_at": data.get("updated_at", ""),
                    "message_count": len(msgs),
                })
            except Exception:
                result.append({"id": sid, "title": sid, "updated_at": "", "message_count": 0})
        result.sort(key=lambda x: x["updated_at"], reverse=True)
        return {"sessions": result}

    @app.delete("/api/sessions/{session_id}")
    async def delete_session(session_id: str):
        agent = get_agent()
        agent.conversation_history.delete(session_id)
        if session_id in _sessions:
            del _sessions[session_id]
        return {"status": "deleted"}

    @app.put("/api/sessions/{session_id}")
    async def rename_session(session_id: str, req: RenameRequest):
        agent = get_agent()
        path = agent.conversation_history.base_path / f"{session_id}.json"
        if not path.exists():
            raise HTTPException(status_code=404, detail="Session not found")
        data = json.loads(path.read_text(encoding="utf-8"))
        data["title"] = req.title
        data["updated_at"] = datetime.now().isoformat()
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        return {"status": "renamed", "title": req.title}

    @app.get("/api/sessions/{session_id}")
    async def get_session(session_id: str):
        agent = get_agent()
        path = agent.conversation_history.base_path / f"{session_id}.json"
        if not path.exists():
            raise HTTPException(status_code=404, detail="Session not found")
        data = json.loads(path.read_text(encoding="utf-8"))
        return data

    return app


async def _stream_chat(agent: Agent, message: str, session_id: str) -> AsyncGenerator[str, None]:
    """SSE 真流式响应：每个 ReAct 步骤即时推送"""
    yield f"data: {json.dumps({'type': 'meta', 'session_id': session_id}, ensure_ascii=False)}\n\n"

    try:
        async for event in agent.loop.execute_stream(message, session_id=session_id):
            etype = event.get("type")
            if etype == "step":
                payload: dict[str, Any] = {"type": "step", "step_index": event.get("step_index")}
                if event.get("thought"):
                    payload["thought"] = event["thought"]
                if event.get("tool_call"):
                    payload["tool_call"] = event["tool_call"]
                if event.get("tool_result"):
                    payload["tool_result"] = event["tool_result"]
                yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
            elif etype == "result":
                yield f"data: {json.dumps({'type': 'result', 'answer': event['answer'], 'total_duration_ms': event['total_duration_ms'], 'total_steps': event['total_steps'], 'success': event['success']}, ensure_ascii=False)}\n\n"
            elif etype == "error":
                err_result = event.get("result")
                answer = err_result.answer if err_result else event.get("message", "")
                yield f"data: {json.dumps({'type': 'error', 'message': answer}, ensure_ascii=False)}\n\n"
    except Exception as e:
        logger.exception("流式响应异常")
        yield f"data: {json.dumps({'type': 'error', 'message': str(e)}, ensure_ascii=False)}\n\n"

    yield "data: [DONE]\n\n"


def run_server(host: str = "localhost", port: int = 8000) -> None:
    import uvicorn
    app = create_app()
    logger.info("启动 Web 服务器: http://%s:%d", host, port)
    uvicorn.run(app, host=host, port=port, log_level="info")
