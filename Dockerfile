# ── Agent Harness 生产镜像 ─────────────────────────────
# 构建：docker build -t agent-harness .
# 运行：docker compose up -d  （挂载 ./data 持久化 SQLite 与向量库）

FROM python:3.11-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    HF_ENDPOINT=https://hf-mirror.com

WORKDIR /app

# 系统依赖（sentence-transformers/torch CPU 轮子无需编译工具链）
RUN apt-get update && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md ./
COPY src ./src

# 依赖层单独缓存（代码变更不触发重装 torch）
RUN pip install -e .

# 本地 BGE 模型直接打进镜像，避免运行时联网下载
COPY models /app/models

COPY scripts ./scripts
COPY data/policies.json data/eval/golden_set.jsonl ./data/
COPY data/seed ./data/seed
COPY src/harness/web/static ./src/harness/web/static

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=120s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4).status==200 else 1)"

# 首次启动自动建库填充数据（幂等），随后启动服务
CMD ["sh", "-c", "python scripts/init_db.py && python -m harness.main --host 0.0.0.0 --port 8000"]
