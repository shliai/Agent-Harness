from __future__ import annotations

from pathlib import Path
from typing import Literal
from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ── LLM（统一 OpenAI v1 兼容接口：OpenAI / 智谱 / DeepSeek / vLLM 等均可）──
    openai_api_key: str = Field(default="", alias="OPENAI_API_KEY")
    openai_api_url: str = "https://api.openai.com/v1"
    openai_model: str = "gpt-4o-mini"

    # ── 生成参数 ─────────────────────────────────────
    temperature: float = 0.3  # 降低随机性，提高响应速度
    max_tokens: int = 2048   # 减少最大token数量

    # ── ReAct 循环 ───────────────────────────────────
    max_iterations: int = 6   # 减少最大迭代次数

    # ── 记忆 ─────────────────────────────────────────
    short_term_window: int = 20
    # 会话压缩：超过阈值条数时把较旧对话用 LLM 压成滚动摘要，
    # LLM 视角 = 摘要 + 最近 keep_recent 条（上下文工程）
    context_compress_enabled: bool = True
    context_compress_threshold: int = 28
    context_keep_recent: int = 16
    context_summary_max_chars: int = 500
    long_term_enabled: bool = False
    long_term_store_path: Path = Path("./data/memory_store")
    long_term_top_k: int = 3
    # 检索距离超过该值的历史视为不相关，不注入 prompt；None 表示不过滤。
    # 注意：旧集合以 L2 空间创建，余弦阈值仅对新建集合（cosine）严格成立，
    # 设为 None 可完全关闭该行为。
    long_term_max_distance: float | None = None

    # ── Guardrails ──────────────────────────────────
    rate_limit_max_requests: int = 60
    rate_limit_window_seconds: int = 60

    # ── 工具重试 ────────────────────────────────────
    tool_max_retries: int = 1

    # ── 知识库 ──────────────────────────────────────
    knowledge_store_path: Path = Path("./data/chroma_db")
    retrieval_top_k: int = 5
    retrieval_candidates: int = 50  # 两阶段检索的候选池大小
    retrieval_relevance_floor: float = 0.85  # 向量距离超过该值触发放宽重查
    rerank_enabled: bool = True  # LLM as Reranker 开关
    rerank_top_n: int = 20  # 进入精排的候选数
    hybrid_search_alpha: float = 0.5  # BM25 关键字通道权重（RRF 融合）

    # ── 可观测性 ────────────────────────────────────
    log_level: str = "INFO"
    log_format: Literal["json", "console"] = "json"
    tracing_enabled: bool = True
    tracer_max_records: int = 500  # 追踪记录上限，防止内存无限增长

    # ── Web ────────────────────────────────────────
    web_host: str = "localhost"
    web_port: int = 8000
    cors_origins: list[str] = Field(default_factory=lambda: ["*"])
    session_cleanup_hours: int = 24  # 会话过期清理（小时）
    token_budget_per_session: int = 120000  # 单会话累计 token 预算
    token_budget_alert_ratio: float = 0.8  # 触发告警的使用比例
    token_budget_hard_stop: bool = False  # 超预算是否硬停（默认仅告警）
    prompt_injection_block: bool = True  # Prompt 注入拦截开关
    llm_max_retries: int = 2  # LLM 瞬时错误重试次数
    llm_retry_backoff_sec: float = 1.0  # 重试基础退避秒数
    stream_include_usage: bool = True  # 流式响应请求返回真实 usage（供应商不支持时可关）
    audit_rotate_mb: int = 16        # 审计日志单文件轮转阈值
    data_dir: Path = Path("./data")
    db_path: Path = Path("./data/harness.db")  # SQLite 业务库（订单/物流/商品/售后）
    admin_token: str = "demo-admin-token"  # 商品管理 API 鉴权 Token（生产务必修改）
    release_channel: str = "stable"  # 发布渠道标识：stable / canary

    @field_validator("log_level")
    @classmethod
    def validate_log_level(cls, v: str) -> str:
        allowed = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
        if v.upper() not in allowed:
            raise ValueError(f"log_level 必须是 {allowed} 之一，收到: {v}")
        return v.upper()

    @field_validator("knowledge_store_path", "long_term_store_path", mode="before")
    @classmethod
    def ensure_path(cls, v: str | Path) -> Path:
        p = Path(v)
        p.mkdir(parents=True, exist_ok=True)
        return p


settings = Settings()
