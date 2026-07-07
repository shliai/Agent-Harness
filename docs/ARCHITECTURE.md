# Agent Harness 架构文档

## 设计思想

Agent = LLM（推理内核）+ Harness（调度外壳）

这是一个电商客服场景的演示系统，LLM 负责文字推理，Harness 负责循环调度、工具执行、状态管理、安全控制和观测埋点。

当前版本: v0.2.0，系统类型: 电商客服场景 ReAct 循环演示系统。

## 核心分层

```
┌─────────────────────────────────────────────────────────────┐
│                     Web 层 (web/api.py)                      │
│           FastAPI + SSE 流式响应 + 静态页面                   │
├─────────────────────────────────────────────────────────────┤
│                    Agent (core/agent.py)                     │
│             组装 LLM + Registry + Guardrails + 记忆           │
├─────────────────────────────────────────────────────────────┤
│     ReAct 循环引擎 (core/loop.py)                            │
│   system prompt → LLM.chat() → 解析 ACTION → 执行工具 → 循环  │
├────────┬────────┬────────┬────────┬────────┬────────────────┤
│ 工具层  │ 记忆层  │ 护栏层  │ 观测层  │ LLM层  │  领域模型     │
│ tools/ │memory/ │guard- │observ-│ llm/  │ domain/       │
│        │        │rails/ │ability│       │               │
├────────┴────────┴────────┴────────┴────────┴────────────────┤
│                    配置层 (config.py)                         │
│              Pydantic Settings + .env 文件                    │
└─────────────────────────────────────────────────────────────┘
```

## 模块职责

### 1. 核心引擎 (`core/`)

**Agent** (`agent.py`)
- 组装 LLM 客户端、工具注册表、护栏流水线、追踪器、指标收集器
- 自动注册 `SubTaskDispatchTool`，并为每个工具注入 LLM 引用
- 对外暴露 `run(user_input, session_id)` 入口

**Registry** (`registry.py`)
- 全局字典管理工具实例，支持注册、获取、列表、描述文本生成
- `get_tool_descriptions()` 动态生成 system prompt 中的工具描述段

**ReActLoop** (`loop.py`)
- 核心循环流程：

```
execute(user_input, session_id):
  1. ShortTermMemory ← 恢复历史会话（如有）
  2. Guardrails.check_input() 校验输入
  3. 组装 system prompt（模板 + 工具描述）
  4. for step in range(MAX_ITERATIONS):
     a. _build_messages() ← system + 历史上下文
     b. LLM.chat() 生成回复
     c. _parse_tool_call() 解析工具调用
        - 搜索 "ACTION:" 前缀
        - json.JSONDecoder.raw_decode 安全解析
     d. 无工具调用 → 返回最终答案
     e. 有工具调用 → Registry.get_tool() → _validate_tool_args() → tool.run()
        - 成功 → Guardrails.check_tool_output() → 写回记忆，继续循环
        - 失败 → 重试 ≤ TOOL_MAX_RETRIES，记录错误让 LLM 修正参数
  5. Guardrails.check_output() 过滤最终答案
  6. ConversationHistory.save() 持久化会话
  7. 返回 AgentResult
```

- system prompt 模板定义在代码中，角色为"小慧"——专业的电商客服助手
- 工具解析：搜索 `ACTION: ` 前缀，使用 `json.JSONDecoder.raw_decode` 安全解析
- 必填参数校验：`tool.spec.parameters.get("required", [])` 中的字段必须存在且非空
- **预算约束规则**（v0.2.0）：system prompt 中显式约束 LLM 行为
  - 用户提具体金额（如"3999的手机"）严格视为预算上限，禁止推荐超预算商品
  - **预算多轮持续生效**：之前对话提过预算，后续推荐同品类商品仍需遵守，除非用户明确变更/取消
  - **调用工具时合并预算到 query**（如用户之前说 3999，现在问"高性能手机"，应传 `query="高性能手机 3999元以内"`）
  - 预算内按价格从高到低排序，优先推荐接近预算上限的款
  - 预算内无匹配时明确告知，再推荐最接近的 2-3 款并标注"略超预算"

### 2. LLM 层 (`llm/`)

**AbstractLLMClient** (`base.py`)
- 抽象接口：
  - `chat()` / `stream_chat()`：同步版本（兼容同步调用场景）
  - `chat_async()` / `stream_chat_async()`：异步版本（v0.2.0 新增，避免阻塞事件循环）

**OpenAICompatibleClient** (`openai_compatible.py`)
- 通过 `httpx` 调用 OpenAI 兼容的 `/chat/completions` 接口
- 支持智谱 AI 和 OpenAI 两种供应商（由配置切换）
- 记录 `last_token_usage` 用于指标统计
- 超时 60s（普通）/ 120s（流式）
- **连接池复用**（v0.2.0）：`httpx.Client` + `httpx.AsyncClient` 全局单例，减少 TCP/TLS 握手开销
- **异步优先**（v0.2.0）：`ReActLoop` 和 `SubTaskDispatchTool` 全部使用 `await llm.chat_async()`，不再阻塞 asyncio 事件循环

**LLMFactory** (`factory.py`)
- 根据 `LLM_PROVIDER` 配置创建对应客户端实例

### 3. 工具系统 (`tools/`)

**BaseTool** (`base.py`)
- 抽象基类：`spec: ToolSpec` + `async def run(**kwargs) -> str`
- ToolSpec 包含 name、description、parameters（JSON Schema 格式）
- `set_llm()` 钩子用于 SubTaskDispatchTool 注入 LLM

**KnowledgeRetrievalTool** (`knowledge_retrieval.py`)
- 本地 ChromaDB PersistentClient + BGE 嵌入模型 (`models/bge-small-zh-v1.5`)
- **BGE 共享**（v0.2.0）：通过 `memory.embeddings.get_embed_fn()` 获取全局单例，与 `LongTermMemory` 共用，避免重复加载
- **Document 自然语言拼接**（v0.2.0）：seed 时 document 不再用 `json.dumps(product)`，改为 `"{name} | {brand}{category} | ¥{price} | {description} | {specs} | 标签：{tags}"`，自然语言文本对 BGE 语义检索匹配度远优于 JSON 字符串
- 检索流程：
  1. `_extract_filters()`: 正则抽取价格范围（`X 以内/X-Y/X 以上` + **v0.2.0 新增"X 的手机/预算 X"等精确预算表达**）和品类
  2. `_build_where()`: 构建 ChromaDB `$and` 过滤条件
  3. `collection.query()`: 向量检索
  4. BM25 重打分 + 向量距离归一化 → 混合得分 `α·BM25_norm + (1-α)·vec_norm`
  5. **预算接近度加权**（v0.2.0）：检测到 `price_max` 时，最终得分 = `0.4·hybrid_score + 0.6·(price/price_max)`，让接近预算上限的商品排前面，避免低价款挤占 top-K
  6. 按最终得分降序取 top-k

**OrderQueryTool** (`order_query.py`)
- 硬编码 MOCK_ORDERS 字典，50 个模拟订单
- 按订单号精确匹配
- 状态分布：已发货(17)、配送中(10)、待发货(9)、已完成(9)

**LogisticsQueryTool** (`logistics_query.py`)
- 硬编码 MOCK_TRACKING 字典，41 个物流单号
- 每个单号 5 个轨迹节点，覆盖发出、中转、派送、签收全流程
- 快递公司：顺丰(SF)、圆通(YT)、申通(STO)、中通(ZTO)

**CalculatorTool** (`calculator.py`)
- 只允许 `0123456789.+-*/()% ` 字符集
- 限制 `eval()` 的全局/局部命名空间为空，防止注入

**SubTaskDispatchTool** (`subtask_dispatch.py`)
- 接收任务列表，为每个子任务创建独立 Registry（只注册该任务需要的工具）
- 调用 LLM 逐个执行子任务，汇总 JSON 结果
- 禁止递归调用自身（`_FORBIDDEN_SUBTOOLS = {"subtask_dispatch"}`）
- 最大迭代次数 = `len(tools) * 2 + 2`

### 4. 记忆系统 (`memory/`)

**embeddings** (`embeddings.py`) — v0.2.0 新增
- 全局单例 BGE 嵌入模型管理器
- `get_embed_fn()`：懒加载 + 缓存，整个进程只加载一次 BGE 模型（约 21 秒）
- `warmup()`：主动触发模型加载，供 `main.py` 启动时调用
- **共享实例**：`KnowledgeRetrievalTool` 和 `LongTermMemory` 共用同一 embedding function，避免重复加载
- **启动预热**：服务启动时先加载 BGE，把 21 秒延迟从请求路径移到启动阶段，首次请求从 48 秒降至 4 秒

**ShortTermMemory** (`short_term.py`)
- 使用 `deque` 滑动窗口，保留最近 N 轮（默认 20）
- 提供 `add()`、`get_context()`、`clear()` 方法
- 作用域：单次 ReAct 循环内的上下文

**LongTermMemory** (`long_term.py`) — v0.2.0 新增
- 基于 ChromaDB 独立 collection `agent_long_term_memory`，复用 BGE 嵌入模型 (`models/bge-small-zh-v1.5`)
- **BGE 共享**（v0.2.0）：通过 `memory.embeddings.get_embed_fn()` 获取全局单例，与 `KnowledgeRetrievalTool` 共用
- 与知识库 `ecommerce_knowledge` 集合隔离，互不污染
- 作用域：跨所有 session_id 的语义记忆
- 三大方法：
  - `async add(user_input, assistant_answer, session_id)`：拼接为 `"用户: ...\n助手: ..."` 单条文档写入
  - `async search(query, top_k)`：语义检索返回 Top-K 相关历史
  - `count()`：返回当前记忆库条数
- **异步化**（v0.2.0）：`add()` / `search()` 内部用 `asyncio.to_thread()` 把同步的 ChromaDB 调用（含 BGE 编码）放到线程池执行，避免阻塞事件循环
- 写入时机：仅在 ReAct 循环**正常完成**后写入一次，失败/超时/护栏拦截不写入（避免噪声）
- **后台写入**（v0.2.0）：`ReActLoop` 中用 `asyncio.create_task()` 后台执行写入，响应不等待写入完成即返回
- 检索时机：每轮对话开始时用当前用户输入做语义查询，结果注入 system prompt 的「相关历史记忆」段落
- 配置驱动：受 `LONG_TERM_ENABLED` 开关控制，默认 `false`，关闭时完全不加载 ChromaDB，零开销
- 安全降级：初始化失败自动 `enabled=False`，所有操作 try/except 兜底，**永远不会**因长期记忆故障影响主流程

**ConversationHistory** (`conversation_history.py`)
- JSON 文件持久化到 `data/conversations/{session_id}.json`
- 自动清理超过 24h 未更新的文件
- 支持 save、load、delete、list_sessions
- 作用域：单个 session_id 的精确恢复

**SessionManager** (`session.py`)
- 会话快照：MD5 哈希生成 session_id，JSON 保存状态
- 用于会话 ID 生成和状态管理

**三层记忆分工对比**

| 层 | 实现 | 作用域 | 检索方式 | 持久化 |
|---|---|---|---|---|
| 短期 | `ShortTermMemory` (deque) | 单次 ReAct 循环 | 顺序读取 | 内存 |
| 会话历史 | `ConversationHistory` (JSON) | 单个 session_id | 精确恢复 | JSON 文件 |
| 长期 | `LongTermMemory` (ChromaDB) | 跨所有 session | 语义相似度 | 向量库 |

> **共享基础设施**（v0.2.0）：长期记忆和知识检索共用 `memory/embeddings.py` 提供的 BGE 单例，避免重复加载模型。

### 5. 安全护栏 (`guardrails/`)

**GuardrailPipeline** (`base.py`)
- 按注册顺序执行护栏检查，首个返回非 None 的结果即停止
- 三个入口：`check_input()`、`check_output()`、`check_tool_output()`

**InputValidator** (`input_validator.py`)
- 拦截空输入、超长输入（>4096 字符）、控制字符（`\x00-\x1f`）
- 不符合条件时抛出 `InputValidationError`

**OutputFilter** (`output_filter.py`)
- 4 条正则匹配并脱敏敏感信息替换为 `***`
- 身份证（18 位）、手机号（11 位 1[3-9]开头）、银行卡（16-19 位）、API Key（sk- 开头）

**RateLimiter** (`rate_limiter.py`)
- 滑动窗口时间戳列表，默认 60 次/60 秒
- 超出时抛出 `RateLimitError`

**AuditLogger** (`audit_logger.py`)
- 所有检查记录写入 `data/audit_logs/audit_{date}.jsonl`
- 按日分文件

### 6. 可观测性 (`observability/`)

**Logger** (`logger.py`)
- 支持 console（带时间/级别信息）和 JSON 两种格式
- 自动静音 httpx、urllib3、langchain 的 DEBUG 日志

**MetricsCollector** (`metrics.py`)
- 计数器：LLM 调用次数、Token 消耗、工具调用次数、总耗时
- 提供 `snapshot()` 和 `summary()` 输出

**Tracer** (`tracer.py`)
- 记录每个 step 的 thought、tool_call、tool_result
- 可通过 `Agent.get_trace_log()` 获取全链路追踪

### 7. Web 层 (`web/`)

**API** (`api.py`)
- FastAPI 应用，全局单例 Agent
- `POST /api/chat` 支持 SSE 流式响应，逐步推送 meta、step、result、[DONE] 事件
- 会话 CRUD：`GET/PUT/DELETE /api/sessions/{id}`
- `GET /api/tools` 返回工具元信息

**前端** (`static/index.html`)
- 纯前端深色主题聊天界面
- 左侧会话列表、右侧聊天面板、可折叠右侧信息栏
- Markdown 渲染、ReAct 步骤卡片、业务字段高亮

## 配置体系

所有配置集中在 `config.py`，通过 `pydantic-settings` 从 `.env` 文件读取：

| 分组 | 关键配置 | 默认值 |
|------|---------|--------|
| **LLM** | provider / api_key / model / temperature / max_tokens | zhipu / — / glm-4 / 0.3 / 2048 |
| **ReAct** | max_iterations / tool_max_retries | 6 / 1 |
| **记忆** | short_term_window / long_term_enabled / long_term_top_k / long_term_store_path | 20 / false / 3 / ./data/memory_store |
| **护栏** | rate_limit_max_requests / rate_limit_window_seconds | 60 / 60 |
| **知识库** | hybrid_search_alpha / retrieval_top_k / knowledge_store_path | 0.5 / 5 / ./data/chroma_db |
| **观测** | log_level / log_format / tracing_enabled | INFO / json / true |

> 配置验证器：`validate_log_level` 限制为 DEBUG/INFO/WARNING/ERROR/CRITICAL 之一；`ensure_path` 自动创建目录。

## 工具注册机制

```python
# 1. 实现 BaseTool
class MyTool(BaseTool):
    spec = ToolSpec(name="my_tool", description="...", parameters={...})
    async def run(self, **kwargs) -> str: ...

# 2. 注册到 Registry
registry.register_tool(MyTool())

# 3. 框架自动将工具描述注入 system prompt
# 4. ReAct 循环按 name 查找并调用
```

## 数据存储

```
data/
├── chroma_db/           # ChromaDB 商品知识库（向量 + 文档，collection: ecommerce_knowledge）
├── memory_store/        # ChromaDB 长期记忆（collection: agent_long_term_memory）— v0.2.0 新增
├── conversations/       # 会话历史 JSON 文件（按 session_id 隔离）
├── sessions/            # 会话快照
├── audit_logs/          # 审计日志 JSONL 文件（按日分文件）
└── products.json        # 商品种子数据源
```

## API 数据流（一次完整请求）

```
用户 POST /api/chat {message, session_id, stream}
  ↓
get_agent() → 全局单例 Agent
  ↓
Agent.run(message, session_id)
  ↓
ReActLoop.execute_stream()  [async]
  ├─ Guardrails.check_input() → InputValidator + RateLimiter + AuditLogger
  ├─ ShortTermMemory 加载历史会话（如有）
  ├─ [若启用] await LongTermMemory.search(user_input) → 语义检索相关历史对话 [asyncio.to_thread]
  │   └─ 命中则注入 system prompt 的「相关历史记忆」段落
  ├─ for step in range(MAX_ITERATIONS):
  │   ├─ await LLM.chat_async(messages) → 智谱 AI / OpenAI [httpx.AsyncClient]
  │   ├─ _parse_tool_call() → 搜索 ACTION: + JSON raw_decode
  │   ├─ 无工具调用 → yield step(thought) → break  [v0.2.0: 直接回答也推送 step]
  │   ├─ Registry.get_tool() → 查找工具
  │   ├─ _validate_tool_args() → 校验必填参数
  │   ├─ await tool.run(**kwargs) → 执行工具
  │   ├─ Guardrails.check_tool_output() → OutputFilter + AuditLogger
  │   ├─ 写回 ShortTermMemory
  │   └─ yield step(thought, tool_call, tool_result) → 继续循环
  ├─ Guardrails.check_output() → OutputFilter
  ├─ ConversationHistory.save() → JSON 持久化
  ├─ [若启用] asyncio.create_task(LongTermMemory.add(...)) → 后台写入向量库 [v0.2.0: 不阻塞响应]
  └─ yield result
  ↓
SSE 流式返回: meta → step(0..N) → result → [DONE]
  ↓
前端渲染消息和工具调用步骤
```

> **v0.2.0 异步化要点**：
> - LLM 调用、长期记忆检索/写入全部走 async 路径，事件循环不再被阻塞
> - SSE step 事件可即时推送，前端不再出现"等待全程跑完才显示步骤"的问题
> - 长期记忆写入用 `create_task` 后台执行，响应不等待写入完成即返回