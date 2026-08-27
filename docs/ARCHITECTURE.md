# Agent Harness 架构文档

> 版本：v0.7.4 · 更新日期：2026-08-25

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
│ 10个  │ 四层架构  │ 6道护栏   │ability/  │ SQLite + │ OpenAI v1   │
│       │          │          │          │ ChromaDB │ 兼容协议     │
├───────┴──────────┴──────────┴──────────┴──────────┴─────────────┤
│            领域模型 domain/ · 配置中心 config.py                    │
└──────────────────────────────────────────────────────────────────┘
```

## 模块职责

### 1. 循环引擎 (`core/loop.py`)

```
execute_stream(user_input, session_id, user_id):
  1. 设置请求上下文(ContextVar: user_id/session_id)
     —— 订单归属校验、工单归属、长期记忆隔离的数据基础
  2. 加载会话状态(messages + chapters + working_memory + traces)
     —— 首次对话时后台调度长期记忆维护(TTL/孤儿/去重/熔断)
  3. Guardrails.check_input()（含审计留痕）
  4. 工作记忆规则抽取（预算/单号/话题）→ 状态尾注
  5. 上下文装配顺序（KV-cache 友好）：
     [system 人设+工具] → [system 冻结章节×k] → [历史消息(只追加)]
     → [system 状态尾注=当期WM+跨会话召回] → [本轮输入]
  6. for step in range(MAX_ITERATIONS):
     a. stream_chat_async 流式生成 → delta 增量即时推送前端
     b. _parse_tool_call()（raw_decode 安全解析）
        - 是工具调用 → delta_reset 回滚临时文本 → 参数校验 → 执行
          （失败让 LLM 携带完整上下文修正重试，TOOL_MAX_RETRIES 次）
        - JSON 坏损  → delta_reset + 纠正提示重试（最多 2 次）
        - 直接回答   → check_output 脱敏（变化则 answer_replace）
                       反问检测写入 awaiting_slot（澄清式多轮）
     c. tracer 记录（带 session_id）+ SSE step 事件
  7. 收尾：压缩判定 → 基础状态事务落盘 → 记忆整理（抽取预筛→小模型抽取→
     WM 合并→长期写入）转入 asyncio 后台任务，立即结束 SSE 流不阻塞响应
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
| `seeds.py` | 种子装载器：62 条 2026 年 3C 目录 + 固定种子订单流（多用户/状态分布/物流轨迹） |

**事实源原则**：SQLite 是商品/订单唯一事实源；ChromaDB 只是可随时全量重建的检索索引。
管理端删除商品 = DB 删除 + 向量 delete 同步；检索 where 恒定附带 `status=在售` 双保险。

### 3. 工具层 (`tools/`) —— 10 个

| 工具 | 类型 | 要点 |
|---|---|---|
| knowledge_retrieval | 读 | 向量+BM25 RRF 融合、中文数量词归一化(万/块/k)、价格品类过滤下推、预算接近度加权、在售状态双保险 |
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
| 长期记忆 | `long_term.py` | 跨会话 | ChromaDB+BGE 语义召回，按 user_id 隔离、距离阈值过滤、后台异步写入 |

会话状态持久化：`conversation_history.py` —— SQLite sessions/session_messages 双表，
事务原子写；旧版 JSON 文件首次使用自动迁移；按 SESSION_CLEANUP_HOURS 过期清理。

### 5. 护栏层 (`guardrails/`) —— 流水线短路

```
check_input:   InputValidator → OutputFilter(跳过) → ComplianceFilter(跳过)
               → RateLimiter(per session_id) → AuditLogger(passed/blocked 全留痕)
check_output:  InputValidator(跳过) → OutputFilter → ComplianceFilter → AuditLogger
check_tool_output: 同 output（子任务分发内部同样生效）
```

| 护栏 | 能力 |
|---|---|
| InputValidator | 空值 / 4096 上限 / 控制字符 |
| OutputFilter | 身份证(含X)/手机号/银行卡/API Key 掩码 |
| ComplianceFilter | 绝对化承诺检测→追加「以官方政策为准」提示并告警；违禁词 * 替换 |
| RateLimiter | 滑动窗口 + per-key 隔离（互不殃及） |
| AuditLogger | passed/blocked 全事件审计；按 AUDIT_ROTATE_MB 大小轮转 |

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

- SSE 五类事件：meta / delta / delta_reset / answer_replace / step / result / error
- 同会话 asyncio.Lock 串行化，防并发读改写互相覆盖
- 商品管理 API：X-Admin-Token 鉴权 + 独立限流（30 次/分钟）
- session_id 白名单 `^[A-Za-z0-9_-]{1,64}$` 防路径穿越

## 数据流示例：一次带售后的多步对话

```
用户"订单20240601003要退货"
 → LLM 抽参 {tool:"order_query", order_id}
 → 工具查 SQLite + 归属校验 ✓ → 返回订单文本
 → LLM 抽参 {tool:"after_sale_apply", type:"退货"}
 → 工具校验状态(已完成✓)/幂等检查 → 状态机落库 待审核
 → LLM 组织话术（含售后单号+时效说明，引用政策口径）
 → 脱敏 → delta 已流式上屏 → result 事件收尾
 → 会话状态+工作记忆+推理轨迹 事务落盘 → 长期记忆后台写入
```

## 部署形态

```bash
# 本地
pip install -e ".[dev]" && python scripts/init_db.py --reindex && python -m harness.main

# Docker（推荐）
docker compose up -d      # ./data 卷持久化，启动自动建库填充
```

CI（GitHub Actions）：ruff 关键规则门禁 → pytest(176) → main 分支镜像构建验证。
