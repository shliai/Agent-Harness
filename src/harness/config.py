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

    # ── 小模型（OpenAI 兼容，旁路低风险调用专用）─────────────────
    # 用于事实抽取 / 检索重排等非关键 LLM 调用，省成本且不占用主模型配额。
    # URL/KEY 留空则继承主配置（默认同一网关）；MODEL 留空 = 未启用小模型，全部走主模型。
    openai_small_api_url: str = ""  # alias OPENAI_SMALL_API_URL
    openai_small_api_key: str = ""  # alias OPENAI_SMALL_API_KEY
    openai_small_model: str = ""    # alias OPENAI_SMALL_MODEL

    # ── 生成参数 ─────────────────────────────────────
    temperature: float = 0.3  # 降低随机性，提高响应速度
    max_tokens: int = 2048   # 减少最大token数量

    # ── ReAct 循环 ───────────────────────────────────
    max_iterations: int = 6   # 减少最大迭代次数

    # ── 记忆 ─────────────────────────────────────────
    # KV-cache 友好：窗口内只追加不淘汰；达触发条件时压缩一次，
    # 压缩后 = [system][摘要消息][最近 keep_recent 条]，前缀稳定仅追加
    # 会话压缩：按「相对模型窗口」触发——单会话当前消息估算 token
    # ≥ context_window_tokens × context_compress_ratio 时，把较旧对话用
    # LLM 压成滚动摘要（独立消息，紧跟 system）。估算基于 estimate_tokens
    # 启发式（非精确），换模型只需调整 window_tokens 即可适配其上下文。
    context_compress_enabled: bool = True
    context_window_tokens: int = 262144   # 所用模型的上下文窗口（token），默认 256k
    context_compress_ratio: float = 0.75  # 触发压缩的窗口占用比例
    context_keep_recent: int = 20
    context_summary_max_chars: int = 2000
    long_term_enabled: bool = False
    long_term_store_path: Path = Path("./data/memory_store")
    long_term_top_k: int = 3
    # 检索距离超过该值的历史视为不相关，不注入 prompt；None 表示不过滤。
    # 注意：旧集合以 L2 空间创建，余弦阈值仅对新建集合（cosine）严格成立，
    # 设为 None 可完全关闭该行为。
    # 余弦距离阈值：BGE 向量 distance = 1 - cos_sim，≤0.45 视为语义相关；
    # None 关闭过滤（不推荐——不相关历史会污染上下文）
    long_term_max_distance: float | None = 0.45
    # 维护策略（启动后首次使用时后台执行一次）
    long_term_ttl_days: int = 90            # 低价值记录保留期；含订单号/金额等标识符的高价值记录豁免
    long_term_dup_distance: float = 0.08    # 近重复判定：与更新记录距离低于此值 → 合并删除旧条
    long_term_max_records: int = 5000       # 容量熔断：超出后按「低价值且最旧」优先淘汰

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
    rerank_small_top_n: int = 8  # 小模型重排候选数（小模型限流严/延迟高，缩小输入量）
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
