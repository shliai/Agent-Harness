# Agent Harness 架构文档

> 版本：v0.7.7 · 更新日期：2026-08-27

## 设计思想

Agent = LLM（推理内核）+ Harness（调度外壳）

这是一个电商客服场景的 Agent 运行时系统。LLM 只负责推理与参数抽取，Harness 负责：
循环调度、工具执行、数据访问、记忆管理、安全护栏与可观测性。

**核心边界原则**：LLM 永远不直接触达数据库或文件系统——它输出结构化的工具调用意图，
由工具层校验（归属/风控/必填参数）后执行真正的 I/O，结果转文本回给 LLM 组织话术。

## 核心分层

```
┌──────────────────────────────────────────────────────────────────┐
│                        Web 层 (web/api.py)                        │
│   FastAPI · SSE 流式事件 · 会话锁 · 商品管理 API(鉴权+限流)          │
├──────────────────────────────────────────────────────────────────┤
│                     Agent 装配层 (core/agent.py)                   │
│       LLM 工厂 + Registry(10工具) + GuardrailPipeline + 记忆       │
├──────────────────────────────────────────────────────────────────┤
│                  ReAct 循环引擎 (core/loop.py)                     │
│   token流式 → 原生function calling → 工具执行(修正重试) → 脱敏 → 持久化  │
├───────┬──────────┬──────────┬──────────┬──────────┬─────────────┤
│ 工具层 │  记忆层   │  护栏层   │  观测层   │  存储层   │   LLM 层    │
│tools/ │ memory/  │guardrails│observ-   │ storage/ │ llm/        │
│ 10个  │ 四层架构  │ 7道护栏   │ability/  │ SQLite + │ OpenAI v1   │
│       │          │          │          │ ChromaDB │ 兼容协议     │
├───────┴──────────┴──────────┴──────────┴──────────┴─────────────┤
│            领域模型 domain/ · 配置中心 config.py                    │
└──────────────────────────────────────────────────────────────────┘
```

## 入口链路

`src/harness/main.py` → `warmup()`（启动预热 BGE 嵌入模型，约 21s）→ `run_web`
→ `harness.web.api.run_server`（FastAPI/uvicorn）。Web 装配层 `web/api.py`
负责注册 9 个工具与 7 道护栏并构造 `Agent`。

## 模块职责

### 1. 循环引擎 (`core/loop.py`)

```
execute_stream(user_input, session_id, user_id):
  1. 设置请求上下文(ContextVar: user_id/session_id)
     —— 订单归属校验、工单归属的数据基础（学习机制为单用户，无需 user_id 隔离）
  2. 加载会话状态(messages + chapters + working_memory + traces)
     —— 学习机制为轮末随会话同步落盘，无独立后台维护任务
  3. Guardrails.check_input()（含审计留痕）
  4. 工作记忆规则抽取（预算/单号/偏好，正则确定性）→ 状态尾注
  5. 上下文装配顺序（KV-cache 友好）：
     [system 人设+工具+记忆层级说明] → [system 冻结章节×k(带第N/M段序)] → [历史消息(只追加)]
     → [system 状态尾注=当期WM槽位+滚动摘要] → [本轮输入]
  6. for step in range(max_iterations)（默认 6，取自 settings.max_iterations）:
     a. 首步确定性前置拦截（模型输出之前执行）：
        - 商品意图命中 `_PRODUCT_INTENT_RE`（品类/品牌/推荐/价格等）且非政策/
          投诉/计算类 → delta_reset + 强制 knowledge_retrieval（防幻觉）
        - 否则指代/进度/投诉/政策可行性/订单号/订单列表/物流/订单状态类输入
          → `_plan_forced_readonly()` 强制 calculator / transfer_human /
          after_sale_query / policy_query / order_query / order_list /
          logistics_query（写操作仍交由模型按指代追问协议执行）
        - 前置拦截命中则直接执行该只读工具，跳过本轮模型生成
     b. 非前置拦截步：stream_chat_async 携带原生 tools 载荷流式生成，
        结构化 tool_calls 经 `tool_call_sink` 返回（原生 OpenAI function calling，
        格式由推理服务保证，无文本/JSON 解析失败面）；delta 增量即时推送前端
     c. 解析 tool_call（仅接受结构化 tool_calls，无调用即视为最终回答）：
        - 有工具调用 → delta_reset 回滚临时文本 → 参数校验 → 执行
          （失败让 LLM 携带完整上下文修正重试，tool_max_retries 次）
        - 直接回答   → check_output 脱敏（变化则 answer_replace）
                       反问检测写入 awaiting_slot（澄清式多轮）
     d. tracer 记录（带 session_id）+ SSE step 事件
  7. 收尾：压缩判定 → 基础状态事务落盘 → 记忆整理（折叠滚动摘要(小模型，门控) →
      确定性学习信号落盘，启用时）转入 asyncio 后台任务，立即结束 SSE 流不阻塞响应
  异常分支：GuardrailError / MaxIterationsExceeded /
           CancelledError（客户端断开也落盘部分状态，不丢对话）
```

关键机制：
- **请求级隔离**：MetricsCollector 每请求独立实例；进程级聚合只增不减
- **脱敏前移**：最终回答先过滤再入记忆/历史，敏感信息无法经上下文回流
- **token 级流式**：delta / delta_reset / answer_replace 三事件协议（见 API.md）

### 2. 存储层 (`storage/`) —— v0.4 起

| 组件 | 职责 |
|---|---|
| `db.py` | SQLite(WAL) 业务库，六张表：products / orders / logistics / aftersale / sessions / session_messages。首连按 db_path 自动建表；短连接模式线程安全 |
| `vector_sync.py` | 向量索引同步服务：`render_product_doc` 全局唯一渲染器（seed/管理API/重建共用）；upsert/delete/reindex_all(prune) |
| `seeds.py` | 种子装载器：400 条 2026 年 3C 目录（data/seed/products.json）+ 固定种子订单流（多用户/状态分布/物流轨迹） |

**事实源原则**：SQLite 是商品/订单唯一事实源；ChromaDB 只是可随时全量重建的检索索引。
管理端删除商品 = DB 删除 + 向量 delete 同步；检索 where 恒定附带 `status=在售` 双保险。

### 3. 工具层 (`tools/`) —— 10 个

| 工具 | 类型 | 要点 |
|---|---|---|
| knowledge_retrieval | 读 | 混合检索：BGE 向量 + BM25 经 RRF 融合（hybrid_search_alpha）；query_enricher 同义/预算/品类扩展多路召回（≤5 变体）；LLM-as-reranker 精排（解析失败回退 RRF 序）；相关性低于阈值自动放宽价格重查；中文数量词归一化(万/块/k)、价格品类过滤下推、预算接近度加权、在售状态双保险；索引富化（类别同义词/标签/特性词降噪、展示剥离富化后缀） |
| calculator | 读 | AST 白名单求值器（仅四则/幂/一元运算，幂运算限界），零注入面 |
| order_query | 读 | SQLite 精确查询 + 归属校验 + 枚举风控熔断 |
| order_list | 读 | 按当前用户列订单（不记得单号的入口） |
| logistics_query | 读 | 物流轨迹查询 + 枚举风控 |
| after_sale_apply | 写 | 售后申请：状态前置校验、幂等防重复、状态机落库 |
| after_sale_query | 读 | 本人售后单进度 |
| policy_query | 读 | 结构化政策库 BM25 命中；未命中明确告知并引导转人工——杜绝编造条款 |
| transfer_human | 写 | 转人工工单落盘 data/tickets/*.jsonl |
| subtask_dispatch | 编排 | 多子任务隔离执行，防递归，输出过护栏 |

工具通过 ContextVar（`tools/context.py`）获取 user_id/session_id，
并共享 EnumerationGuard（同会话连续 8 次未命中即熔断 30 分钟）。


### 4. 记忆层 (`memory/`) —— 四层架构

| 层 | 模块 | 生命周期 | 解决的问题 |
|---|---|---|---|
| 工作记忆 | `working_memory.py` | 跨轮持久 | 预算/单号/话题等关键事实的结构化槽位；规则抽取零 LLM 开销；窗口截断也不遗忘 |
| 短期记忆 | `short_term.py` | 跨轮持久 | 原始消息只追加（无滑动淘汰）；track_full 模式同时保留全量供落盘 |
| 会话压缩 | loop 内 `_summarize` | 跨轮持久 | 单会话消息估算 token ≥ 窗口×比例（相对模型上下文）时 LLM 章节式滚动摘要，LLM 视角 = 冻结章节 + 最近 KEEP_RECENT 条 |
| 学习机制 | `learning.py` + `working_memory.py` | 跨会话 | 默认关闭（`learning_enabled=False`）；启用后轮末读取工作记忆的确定性信号（偏好/约束/纠正）→ JSON 文件存储 → 全量注入系统提示词。单用户、无向量、无 LLM 自由抽取；按 (type,key) 合并，纠正权威 > 偏好 |

会话状态持久化：`conversation_history.py` —— SQLite sessions/session_messages 双表，
事务原子写；旧版 JSON 文件首次使用自动迁移；按 SESSION_CLEANUP_HOURS 过期清理。

### 5. 护栏层 (`guardrails/`) —— 流水线短路

```
流水线按装配顺序执行全部护栏（每护栏按 context.type 自行判定是否生效，不生效则放行）：
  InputValidator → InjectionGuard → OutputFilter → ComplianceFilter
  → SystemPromptGuard → RateLimiter → AuditLogger

- check_input:      InputValidator / InjectionGuard / RateLimiter 生效，其余放行
- check_output:     OutputFilter / ComplianceFilter / SystemPromptGuard 生效
- check_tool_output: 同 output（子任务分发内部同样生效）
```

| 护栏 | 阶段 | 能力 |
|---|---|---|
| InputValidator | input | 空值 / 4096 上限 / 控制字符 |
| InjectionGuard | input | 指令注入特征检测（中英文），命中即拦截；受 `settings.prompt_injection_block` 开关控制（默认开） |
| OutputFilter | output/tool_output | 身份证(含X)/手机号/银行卡/API Key 掩码；无命中返回 None 放行（不重写文本，避免误伤后续 SystemPromptGuard） |
| ComplianceFilter | output/tool_output | 绝对化承诺检测→追加「以官方政策为准」提示并告警；违禁词 * 替换 |
| SystemPromptGuard | output | 指纹比对系统提示词特异片段（人设首句/内部章节标题），命中即重写为标准拒答，确定性拦截提示词泄露 |
| RateLimiter | 全 | 滑动窗口 + per-key 隔离（互不殃及） |
| AuditLogger | 全 | passed/blocked 全事件审计；按 AUDIT_ROTATE_MB 大小轮转 |

拦截事件由流水线回调补记（护栏 raise 前短路也能留痕）。

### 6. LLM 层 (`llm/`)

统一 OpenAI v1 兼容协议（v0.5 移除供应商分支）：
OpenAI / 智谱 / DeepSeek / 通义 / 本地 vLLM 仅需配置
`OPENAI_API_URL / OPENAI_API_KEY / OPENAI_MODEL` 三项。
可选小模型三件套 `OPENAI_SMALL_API_URL / KEY / MODEL`：事实抽取、检索重排等
旁路低风险调用优先走小模型省成本（URL/KEY 留空继承主网关；MODEL 留空自动回落主模型）。

- `chat_async` → LLMReply(content, total_tokens)——token 用量随调用返回，并发不串号
- `stream_chat_async` → token 级增量（主循环使用）
- max_tokens / temperature 正确下发；temperature=0 不被吞

### 7. Web 层 (`web/api.py`)

- SSE 七类事件：meta / delta / delta_reset / answer_replace / step / result / error
- 同会话 asyncio.Lock 串行化，防并发读改写互相覆盖
- 管理接口需 `x-admin-token` 请求头鉴权 + 独立限流（每 Token 30 次/分钟）
- session_id 白名单 `^[A-Za-z0-9_-]{1,64}$` 防路径穿越

端点清单：
- GET  `/` · `/health` · `/api/tools` · `/api/metrics`
- GET  `/api/sessions` · `/api/sessions/{sid}`（会话列表 / 详情）
- GET  `/api/admin/products` · `/api/admin/aftersales`（管理：商品 / 售后列表）
- POST `/api/chat`（SSE 流式）· `/api/session/clear`
- POST `/api/sessions/batch-delete`
- POST `/api/admin/products`（新增）· `/api/admin/products/reindex`（全量重建向量库）
- POST `/api/admin/aftersales/{id}/approve|reject|complete`（售后审核）
- PUT  `/api/admin/products/{pid}` · `/api/sessions/{sid}`（改商品 / 改会话标题）
- DELETE `/api/admin/products/{pid}` · `/api/sessions/{sid}`（删商品 / 删会话）

## 数据流示例：一次带售后的多步对话

```
用户"订单20240601003要退货"
 → 原生 function calling 触发 order_query（order_id）
 → 工具查 SQLite + 归属校验 ✓ → 返回订单文本
 → 原生 function calling 触发 after_sale_apply（type:"退货"）
 → 工具校验状态(已完成✓)/幂等检查 → 状态机落库 待审核
 → LLM 组织话术（含售后单号+时效说明，引用政策口径）
 → 脱敏 → delta 已流式上屏 → result 事件收尾
 → 会话状态+工作记忆+推理轨迹 事务落盘 → 学习机制落盘（启用时）
```

## 部署形态

```bash
# 本地
pip install -e ".[dev]" && python scripts/init_db.py --reindex && python -m harness.main

# Docker（推荐）
docker compose up -d      # ./data 卷持久化，启动自动建库填充
```

CI（GitHub Actions）：ruff 关键规则门禁 → pytest(176) → main 分支镜像构建验证。
