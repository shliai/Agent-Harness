from __future__ import annotations

import sys
import argparse

from harness.config import settings
from harness.memory.embeddings import warmup
from harness.observability.logger import setup_logging


def main() -> None:
    parser = argparse.ArgumentParser(description="Agent Harness — 智能体运行时外壳")
    parser.add_argument("--port", type=int, default=settings.web_port,
                        help=f"Web 服务端口 (默认 {settings.web_port})")
    parser.add_argument("--host", type=str, default=settings.web_host,
                        help=f"Web 服务监听地址 (默认 {settings.web_host})")
    args = parser.parse_args()

    setup_logging(level=settings.log_level, fmt=settings.log_format)
    # 启动时预热 BGE 嵌入模型（约 21 秒），避免首次请求时阻塞
    # 模型加载完成后，KnowledgeRetrievalTool 和 LongTermMemory 共享同一实例
    warmup()
    run_web(host=args.host, port=args.port)


def run_web(host: str = "0.0.0.0", port: int = 8000) -> None:
    from harness.web.api import run_server
    run_server(host=host, port=port)


if __name__ == "__main__":
    sys.exit(main())
