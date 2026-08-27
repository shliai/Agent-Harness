# 运维手册（Operations）

> 版本：v0.7.7 · 面向部署/值班/SRE 视角的运行指南

## 部署与启动（run/deploy）

- **启动命令**：
  ```
  python -m harness.main --host <host> --port <port>
  ```
  默认 `--host localhost --port 8000`（由 `WEB_HOST` / `WEB_PORT` 决定），内部调用 `harness.web.api.run_server`。
- **启动流程**：
  1. 初始化日志（见 §1）。
  2. **预热 BGE 嵌入模型（约 21 秒）**：加载本地 `models/bge-small-zh-v1.5`，避免首个请求阻塞。启动期间服务不可用，请预留就绪时间；就绪检查可轮询 `GET /health`。
  3. 预热 Agent（工具注册 + ChromaDB 初始化）。
- **依赖**：`.env` 必须配置 `OPENAI_API_KEY`（及 `OPENAI_API_URL` / `OPENAI_MODEL` 视需要）。`HF_HUB_OFFLINE=True` 默认开启，表示**不发起任何 HuggingFace 网络调用**（模型走本地路径），离线/内网环境可正常启动。
- **无状态**：应用层无状态，多副本横向扩容即可；注意限流/风控计数、会话锁均为**进程内**实现，多副本需迁移至 Redis（接口已预留，见 `guardrails/rate_limiter.py`）。
- **存储**：业务库 SQLite `./data/harness.db`（订单/物流/商品/售后），向量库 Chroma `./data/chroma_db`。首次部署请运行 `python scripts/init_db.py --reindex` 建表并同步向量索引。

## 1. 日志聚合

应用日志**双路输出**：本地可读性（stderr）+ 文件持久化（JSON，按天轮转）：

- **stderr**：`LOG_FORMAT=json` 输出结构化日志，容器化后由采集端接管：
  ```
  容器 stdout/stderr → Docker logging driver / Filebeat sidecar → Loki | ELK
  ```
  本地调试用 `LOG_FORMAT=console`。
- **文件**：`LOG_DIR/harness.log`（默认 `./data/logs`），TimedRotatingFileHandler 按天切割，保留 `LOG_BACKUP_DAYS`（默认 7）天；统一 JSON 格式便于机器回溯/聚合。

关键检索字段：`logger`（模块）、`session_id`、`level`；错误堆栈在 `exception` 字段。

- **session_id 关联**：API 层每轮请求注入 `session_id`（ContextVar），同轮内所有日志——包括 `asyncio.create_task` 派生的后台记忆整理任务——自动携带该字段，可按会话过滤排查。
- **逐次耗时埋点**：每次 LLM 调用（流式/非流式）与工具执行均记录单次耗时（毫秒），`loop` 模块含步骤索引与 token 数，可用于定位慢会话/慢工具。
- 审计流水独立落盘 `data/audit_logs/*.jsonl`（含 PII 掩码），按 `AUDIT_ROTATE_MB`（默认 16 MB）轮转，
  建议 Filebeat 单独 tail 到保留策略更长的索引。

## 2. 指标与告警

### 应用内指标端点

`GET /api/metrics` 返回 JSON：

```json
{
  "metrics": { "...": "进程级聚合指标（累计 LLM 调用、token、耗时、工具计数、uptime 等）" },
  "recent_traces": [ "... 最近 50 条步骤记录（带 session_id）..." ]
}
```

> 注意：当前**未提供 Prometheus 抓取端点**（无 `/metrics/prometheus`）。如需 Prometheus 采集，需自行桥接 `/api/metrics` 或后续接入 exporter。

### 应用内预算告警

单会话 token 用量达 `TOKEN_BUDGET_PER_SESSION × TOKEN_BUDGET_ALERT_RATIO`（默认 `120000 × 0.8`）时，
输出 `[ALERT][BUDGET]` WARNING 日志——告警管道对该关键字建立规则即可联动；
达到 100% 且 `TOKEN_BUDGET_HARD_STOP=true` 时会话被优雅终止（默认 `false`，仅告警不硬停）。

## 3. 灰度发布（预留能力）

- `RELEASE_CHANNEL` 配置项（stable/canary）已定义于 config，但当前代码暂未消费（不实际下发响应头）
- 功能落地后推荐路径：
  1. 构建两个镜像 tag（stable / canary）
  2. 反向代理按权重分流（Traefik/Nginx 示例：canary weight=10%）
  3. 观察 canary 的 token 增速、工具失败率、badcase 导出结果后再放量
- 应用层无状态，扩容 = 多副本；注意限流/风控计数器为进程内实现，
  多副本部署时需迁移至 Redis（接口已预留，见 `guardrails/rate_limiter.py`）

## 4. 密钥管理

主模型密钥从环境变量 `OPENAI_API_KEY` 读取（OpenAI v1 兼容协议统一入口，未实现文件挂载）。

可选小模型 `OPENAI_SMALL_API_KEY` 同理（URL/KEY 留空时继承主配置）；`OPENAI_SMALL_MODEL`
留空则旁路调用（事实抽取/重排）自动回落主模型，成本与主模型一致。

规范：

- `.env` 已列入 `.gitignore`，严禁提交真实密钥
- K8s 生产示例（Secret 注入为环境变量）：
  ```yaml
  env:
    - name: OPENAI_API_KEY
      valueFrom:
        secretKeyRef:
          name: openai-secret
          key: api-key
  ```
- 轮换：更新 Secret 后滚动重启进程即可（客户端启动时读取一次）

> **管理员令牌（务必修改）**：`/api/admin/*` 全部接口通过 `X-Admin-Token` 请求头鉴权，
> 令牌来自 `ADMIN_TOKEN`（默认 `demo-admin-token`）。**生产环境必须改为强随机值**，
> 否则任何人均可管理商品与售后。鉴权失败返回 403，管理接口独立限流（30 次/分钟/Token）超限返回 429。

## 5. 数据生命周期

| 数据 | 位置 | 保留策略 |
|---|---|---|
| 会话状态 | SQLite sessions/messages 表 | `SESSION_CLEANUP_HOURS`（默认 24h）自动清理 |
| 商品/订单/物流 | SQLite | 种子或上游同步为准；`python scripts/init_db.py --reset --reindex` 可全量重建 |
| 学习机制 | JSON `./data/learning_store/learning.json` | `LEARNING_ENABLED` 默认 **false（关闭）**；开启后轮末确定性捕获偏好/约束/纠正并持久累积（单用户画像，无向量） |
| 审计流水 | audit_logs/*.jsonl | 按 `AUDIT_ROTATE_MB`（默认 16 MB）轮转；合规留存期由组织策略定义 |

## 6. 故障排查速查

| 症状 | 排查入口 |
|---|---|
| 启动后长时间无响应 / 首请求慢 | 启动时 BGE 模型预热约 21s；就绪前请等待或轮询 `GET /health` |
| 启动报 HuggingFace 网络错误 | `HF_HUB_OFFLINE` 默认 true，模型走本地路径；勿误改为 false 触发联网 |
| 回复中出现「系统异常」 | stderr 日志搜 `Agent 执行异常` |
| 大量 429（管理接口） | 管理接口独立限流（30 次/分钟/Token）命中，检查调用方重试逻辑 |
| 大量 429（聊天/小模型） | 若集中在事实抽取/重排时段，多为小模型限流——旁路调用已由 `cheap_semaphore(3)` 限制并发，仍打满可降低 `OPENAI_SMALL_*` 网关并发配额或增大重排冷却 |
| 检索返回「已放宽价格条件」 | 正常自校正行为；频繁出现说明目录覆盖不足 |
| `[ALERT][BUDGET]` 密集 | 单用户滥用或长会话未压缩，评估调低阈值 |
| 向量库计数与 DB 不符 | `python scripts/init_db.py --reindex` 对账重建 |

## 7. 配置项参考（config.py · Settings）

读取 `.env`（大小写不敏感）。以下为真实存在的配置项与默认值；未标注默认值的为必填或路径类。

**LLM / 生成**
| 配置项 | 默认 | 说明 |
|---|---|---|
| OPENAI_API_KEY | （必填） | 主模型密钥 |
| OPENAI_API_URL | `https://api.openai.com/v1` | 主模型网关 |
| OPENAI_MODEL | `gpt-4o-mini` | 主模型名 |
| OPENAI_SMALL_API_URL / OPENAI_SMALL_API_KEY / OPENAI_SMALL_MODEL | `""` | 留空则禁用小模型、回落主模型 |
| TEMPERATURE | `0.3` | 采样温度 |
| MAX_TOKENS | `2048` | 单次生成上限 |
| MAX_ITERATIONS | `6` | ReAct 最大步数 |
| LLM_MAX_RETRIES | `2` | LLM 调用重试 |
| LLM_RETRY_BACKOFF_SEC | `1.0` | 重试退避秒数 |
| STREAM_INCLUDE_USAGE | `True` | 流式是否附带 usage |

**上下文 / 记忆**
| 配置项 | 默认 | 说明 |
|---|---|---|
| CONTEXT_COMPRESS_ENABLED | `True` | 超窗压缩 |
| CONTEXT_WINDOW_TOKENS | `262144` | 上下文窗口 |
| CONTEXT_COMPRESS_RATIO | `0.75` | 压缩比例 |
| CONTEXT_KEEP_RECENT | `20` | 保留最近条数 |
| CONTEXT_SUMMARY_MAX_CHARS | `2000` | 摘要最大字符 |
| LEARNING_ENABLED | `False` | 学习机制（默认关闭） |
| LEARNING_STORE_PATH | `./data/learning_store` | 学习记忆 JSON 存储目录 |
| LEARNING_TTL_DAYS | `365` | 学习记录保留期（天） |
| LEARNING_MAX_ITEMS | `50` | 单用户画像容量上限 |
| LEARNING_CONFIDENCE_THRESHOLD | `0.0` | 置信度阈值（占位） |

**检索 / 向量**
| 配置项 | 默认 | 说明 |
|---|---|---|
| KNOWLEDGE_STORE_PATH | `./data/chroma_db` | 向量库路径 |
| RETRIEVAL_TOP_K | `5` | 最终召回 |
| RETRIEVAL_CANDIDATES | `50` | 候选集 |
| RETRIEVAL_RELEVANCE_FLOOR | `0.85` | 相关性下限 |
| RERANK_ENABLED | `True` | 重排开关 |
| RERANK_TOP_N | `20` | 重排候选 |
| RERANK_SMALL_TOP_N | `8` | 小模型重排候选 |
| HYBRID_SEARCH_ALPHA | `0.5` | 混合检索权重 |
| HF_HUB_OFFLINE | `True` | 禁用 HF 联网（本地模型） |

**限流 / 护栏**
| 配置项 | 默认 | 说明 |
|---|---|---|
| RATE_LIMIT_MAX_REQUESTS | `60` | 普通接口限流/窗口 |
| RATE_LIMIT_WINDOW_SECONDS | `60` | 限流窗口秒 |
| TOOL_MAX_RETRIES | `1` | 工具重试 |
| PROMPT_INJECTION_BLOCK | `True` | 注入拦截 |

**Web / 会话**
| 配置项 | 默认 | 说明 |
|---|---|---|
| WEB_HOST | `localhost` | 监听地址 |
| WEB_PORT | `8000` | 监听端口 |
| CORS_ORIGINS | `["*"]` | 跨域白名单 |
| SESSION_CLEANUP_HOURS | `24` | 会话清理周期 |
| TOKEN_BUDGET_PER_SESSION | `120000` | 单会话 token 预算 |
| TOKEN_BUDGET_ALERT_RATIO | `0.8` | 预算告警比例 |
| TOKEN_BUDGET_HARD_STOP | `False` | 超预算硬停 |

**日志 / 审计**
| 配置项 | 默认 | 说明 |
|---|---|---|
| LOG_LEVEL | `INFO` | 枚举校验 |
| LOG_FORMAT | `json`/`console` | 日志格式 |
| LOG_DIR | `./data/logs` | 日志目录 |
| LOG_BACKUP_DAYS | `7` | 日志保留天数 |
| TRACING_ENABLED | `True` | 追踪开关 |
| TRACER_MAX_RECORDS | `500` | 追踪最大条数 |
| AUDIT_ROTATE_MB | `16` | 审计日志轮转 MB |

**存储 / 安全**
| 配置项 | 默认 | 说明 |
|---|---|---|
| DATA_DIR | `./data` | 数据根目录 |
| DB_PATH | `./data/harness.db` | 业务 SQLite |
| ADMIN_TOKEN | `demo-admin-token` | 管理令牌（**生产必改**） |
| RELEASE_CHANNEL | `stable` | 灰度通道（当前未消费） |
