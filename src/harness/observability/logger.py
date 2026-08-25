from __future__ import annotations

import json
import logging
import logging.handlers
import sys
from contextvars import ContextVar
from datetime import datetime, timezone
from pathlib import Path

# 当前请求会话上下文：由 API 层在每轮请求开始时注入，
# 使同轮所有日志（含后台记忆整理任务）自动携带 session_id，便于按会话关联排查。
# ContextVar 天然按 async 任务隔离；asyncio.create_task 会复制上下文，后台任务也能读到。
current_session_id: ContextVar[str | None] = ContextVar("session_id", default=None)


def set_session_id(session_id: str | None) -> None:
    """为当前请求/任务上下文设置会话标识（传 None 表示退出时清除）"""
    current_session_id.set(session_id)


class SessionFilter(logging.Filter):
    """把当前上下文里的 session_id 挂到日志记录上（json/console 通用）。

    必须保证 record 上始终存在 session_id / session_tag 两个属性，
    console 格式化器才会安全引用 %(session_tag)s。
    """

    def filter(self, record: logging.LogRecord) -> bool:
        sid = current_session_id.get() or ""
        record.session_id = sid
        record.session_tag = f" [{sid}]" if sid else ""
        return True


class JsonFormatter(logging.Formatter):
    """结构化 JSON 日志格式化器"""

    def format(self, record: logging.LogRecord) -> str:
        log_entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        sid = getattr(record, "session_id", "") or ""
        if sid:
            log_entry["session_id"] = sid
        if record.exc_info and record.exc_info[0]:
            log_entry["exception"] = self.formatException(record.exc_info)
        return json.dumps(log_entry, ensure_ascii=False)


def setup_logging(
    level: str = "INFO",
    fmt: str = "console",
    log_dir: Path | None = None,
    backup_days: int = 7,
) -> None:
    root_logger = logging.getLogger()
    root_logger.setLevel(level.upper())
    root_logger.handlers.clear()

    # stderr：保持原可读性（json / console 可切换）
    stderr_handler = logging.StreamHandler(sys.stderr)
    stderr_handler.setFormatter(
        JsonFormatter() if fmt == "json" else logging.Formatter(
            fmt="%(asctime)s [%(levelname)-7s] %(name)s%(session_tag)s: %(message)s",
            datefmt="%H:%M:%S",
        )
    )
    handlers: list[logging.Handler] = [stderr_handler]

    # 文件持久化：按天轮转、保留 backup_days 天；统一 JSON 便于机器回溯/聚合
    if log_dir:
        log_dir = Path(log_dir)
        log_dir.mkdir(parents=True, exist_ok=True)
        file_handler = logging.handlers.TimedRotatingFileHandler(
            log_dir / "harness.log",
            when="midnight",
            backupCount=max(int(backup_days), 1),
            encoding="utf-8",
        )
        file_handler.setFormatter(JsonFormatter())
        handlers.append(file_handler)

    for h in handlers:
        h.addFilter(SessionFilter())
        root_logger.addHandler(h)

    # 关闭第三方库的冗长日志
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
