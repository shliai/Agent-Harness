# Agent Harness 功能实现说明

> 版本：v0.7.4 · 面向开发者的功能实现细节文档
> 架构总览见 [ARCHITECTURE.md](ARCHITECTURE.md) · 接口定义见 [API.md](API.md)

---

## 目录

- [前端实现](#前端实现)
  - [整体架构](#整体架构)
  - [流式对话](#流式对话)
  - [Markdown 渲染器](#markdown-渲染器)
  - [思考时间线](#思考时间线)
  - [会话管理](#会话管理)
  - [商品管理面板](#商品管理面板)
  - [售后审核面板](#售后审核面板)
  - [指标面板](#指标面板)
  - [主题系统](#主题系统)
- [后端实现](#后端实现)
  - [ReAct 循环引擎](#react-循环引擎)
  - [LLM 客户端层](#llm-客户端层)
  - [工具系统](#工具系统)
    - [商品检索](#商品检索-knowledge_retrieval)
    - [订单查询](#订单查询-order_query--order_list)
    - [物流查询](#物流查询-logistics_query)
    - [售后管理](#售后管理-after_sale_apply--after_sale_query)
    - [政策检索](#政策检索-policy_query)
    - [转人工](#转人工-transfer_human)
    - [计算器](#计算器-calculator)
    - [子任务分发](#子任务分发-subtask_dispatch)
  - [记忆体系](#记忆体系)
    - [工作记忆](#工作记忆-working_memory)
    - [短期记忆与会话压缩](#短期记忆与会话压缩)
    - [长期记忆](#长期记忆-long_term_memory)
  - [安全护栏](#安全护栏)
    - [输入校验与注入防护](#输入校验与注入防护)
    - [输出脱敏与合规过滤](#输出脱敏与合规过滤)
    - [限流与审计](#限流与审计)
  - [存储层](#存储层)
    - [SQLite 业务库](#sqlite-业务库)
    - [向量库同步](#向量库同步)
  - [评测框架](#评测框架)

---

## 前端实现

前端为**零依赖单文件应用**（`src/harness/web/static/index.html`），无框架、无构建步骤、无 CDN 引用，所有 HTML/CSS/JS 内联在一个文件中。

### 整体架构

```
布局：flex 弹性方案，非 grid 固定列
├─ .side   固定宽 250px，<880px 时变为 fixed 浮层 + transform 滑入滑出
├─ .main   flex:1 占满剩余空间，内部纵向排列 topbar/chat/compzone
└─ .drawer fixed 右侧浮层 width:min(400px,100vw)，transform 平移进出
```

响应式策略：
- 高度使用 `100vh` + `100dvh` 双声明（dvh 解决移动端地址栏遮挡）
- 关键间距用 `clamp(12px,4vw,22px)` 流式缩放
- `<880px` 断点触发侧栏浮层化和按钮文字隐藏
- 所有容器设置 `min-width:0` 防止 flex 子元素溢出

### 流式对话

SSE 五类事件由 `send()` 函数内的事件分发器统一处理：

| 事件 | 前端行为 |
|---|---|
| `meta` | 更新 `sessionId` 并写入 localStorage |
| `delta` | 将增量文本追加到当前气泡，重新渲染 Markdown |
| `delta_reset` | 清空气泡（该轮实为规划思考而非回答） |
| `step` | 清空气泡 → 在时间线卡片中追加 Thought/Action/Observation |
| `answer_replace` | 整体覆盖气泡内容（脱敏改变了原文时触发） |
| `result` | 渲染最终 Markdown 回答 + 统计信息行 |
| `error` | 显示错误提示 |

核心设计——**单气泡生命周期**：

```
发送 → 创建一个 .bub 元素
  delta → 追加文本 + 光标闪烁
  step  → 清空正文（思考内容移入时间线卡片）
  result→ 替换为最终 Markdown + meta 行
```

全程复用同一个 DOM 元素，不存在"打字气泡/正式气泡"两个元素间的切换问题。

中止处理：
- 用户点击停止 → `AbortController.abort()` → fetch 抛出 AbortError
- catch 分支保留已流式输出的部分内容，追加「⏹ 已停止生成」
- 服务端收到断开后保存部分对话到数据库

### Markdown 渲染器

自研零依赖渲染器（约 80 行），安全性通过 **先转义再格式化** 保证：

```
原始文本
  ↓ 提取代码块占位符（防止代码被格式化）
  ↓ HTML 转义（& < > " '）
  ↓ 行级解析（标题/列表/表格/引用/水平线/段落）
  ↓ 行内格式化（粗体/斜体/删除线/行内代码/链接）
  ↓ 还原代码块（esc 后的代码放入 <pre><code>）
```

支持的语法：

| 语法 | 渲染为 |
|---|---|
| `# ~ ####` | h3 标题 |
| `- item` / `* item` | 无序列表 |
| `1. item` | 有序列表 |
| `\| col \| col \|` | 表格（带 thead/tbody） |
| `> text` | blockquote |
| `---` | hr 分割线 |
| `**text**` | strong |
| `*text*` | em |
| `` `code` `` | inline code |
| `[text](url)` | a 链接（target=_blank） |
| ``` ```lang ``` | pre>code 代码块 |

CSS 补充了 table/blockquote/hr 的样式使其在深色主题下可读。

### 思考时间线

每次发送创建一个 `.trace` 容器，包含：

- **折叠开关**：显示 spinner + 「推理中…」，完成后变为 ✓ + 步数统计
- **步骤卡片列表**：每步一张卡片，含类型标签（TOOL/THINK）、摘要行、展开后的 Thought/Action/Observation 三段

有 TOOL 调用的步骤自动展开 Observation 区域。历史回放时以只读方式重现已保存的轨迹。

双保险机制：
1. `result` / `error` 事件到达时调用 `endTrace(tr)` 正常收尾
2. `finally` 块无条件调用 `endTrace(tr)` 兜底
3. 页面加载时 `reviveStaleTraces()` 扫描并标记遗留的「推理中」卡片为已结束

### 会话管理

状态变量：

```js
sessionId     // 当前活跃会话 ID（localStorage 持久化）
viewingSid    // 当前正在浏览的会话（可能与正在生成的不同）
uid           // 用户身份标识（localStorage 持久化）
selSet        // 批量选中的会话 ID 集合
bulkMode      // 是否处于批量管理模式
lastSessIds   // 最近一次加载的会话 ID 列表
__liveSid     // 当前正在生成的目标会话 ID
```

关键交互路径：

| 操作 | 实现 |
|---|---|
| 新对话 | 清空 localStorage sid → 重置视图为欢迎页 |
| 切换会话 | GET 详情 → 渲染消息+工作记忆卡+历史轨迹；若该会话正在后台生成则重建实时气泡绑定 |
| 重命名 | PUT title → 刷新列表 |
| 删除单个 | DELETE（生成中禁止） |
| 多选删除 | 管理模式 → 勾选 → POST batch-delete → 当前被删则回到新对话 |
| 全选删除 | 进入管理模式 → 全选 → 同上 |
| 清空当前 | POST clear → 重置为欢迎页 |

跨用户保护：chat 写入前检查归属，不匹配返回 403。

### 商品管理面板

入口：顶栏「管理」按钮。需输入管理员 Token（默认 `demo-admin-token`），存储在 localStorage 自动填充。

面板结构（Tab 切换）：

**商品库 Tab**
- 工具栏：搜索框（名称/编号模糊匹配）· 类别下拉 · 状态下拉 · 重建向量索引按钮
- 列表：分页 15 条/页，每行含名称/价格/类别/品牌/编号/状态标签 + 库存指示器
- 操作：编辑（展开表单修改全字段含库存数量）· 上架/下架切换 · 删除（含向量索引同步清除）
- 新增：折叠表单，提交后写 SQLite + 向量库 upsert 联动

数据流：
```
前端操作 → PUT/POST /api/admin/products → SQLite upsert → vector_sync.upsert_products
                                                        → ChromaDB metadata 含 stock/status
```

搜索和筛选在前端内存执行（400 条全量加载到 prodCache 数组），分页纯客户端。

**售后审核 Tab**
- 列表按状态筛选，每条显示售后单号/类型/订单号/件数/预估退款/原因
- 待审核单提供 通过/驳回 操作（驳回必填原因）
- 已通过单提供 完成打款 操作

### 售后审核面板

同上 Tab，调用 `/api/admin/aftersales` 系列端点。待审核单展示通过/驳回按钮，已通过单展示完成打款按钮。

### 指标面板

从 `/api/metrics` 获取 JSON 数据渲染：

- 四格统计卡：LLM 调用次数 · Token 消耗 · 累计时长 · 运行时间
- 工具调用分布：CSS 柱状图（宽度 = 调用次数/最大值 × 100%）
- 最近推理轨迹：最近 8 条的会话 ID + 思考摘要 + 工具名
- 系统状态：长期记忆开关/条数、知识库商品数（来自 /health）

### 主题系统

CSS 自定义属性双套配色，通过 `html[data-theme]` 属性切换：

```css
:root,[data-theme=dark]{ --bg:#0a0a0b; --tx:#ededf0; ... }
[data-theme=light]{ --bg:#fafafa; --tx:#17171a; ... }
```

顶栏 ◑ 按钮切换，偏好存入 localStorage。默认跟随 `prefers-color-scheme`。

---

## 后端实现

### ReAct 循环引擎

文件：`src/harness/core/loop.py`

#### execute_stream() 主流程

```python
async def execute_stream(user_input, session_id, user_id):
    # 1. 设置请求上下文（ContextVar: user_id/session_id/budget）
    # 2. 加载会话状态（messages + summary + working_memory + traces）
    # 3. guardrails.check_input() → 审计留痕
    # 4. 工作记忆规则抽取 → 注入 system prompt
    # 5. 上下文装配（摘要 → WM → 长期召回 → 窗口消息）
    # 6. for step in range(max_iterations):
    #      stream_chat_async() → yield delta 增量
    #      parse_tool_call()
    #      ├─ 有 ACTION → delta_reset → 校验 → 执行工具 → 结果入记忆
    #      ├─ JSON 坏损 → delta_reset → 纠正提示重试（≤2 次）
    #      └─ 无 ACTION → check_output 脱敏 → 反问检测 → result
    # 7. 收尾：压缩判定 → asave_state → 长期记忆后台写入
    #
    # 异常分支：
    #   GuardrailError → error 事件
    #   MaxIterationsExceeded → 友好提示
    #   CancelledError → 保存部分状态后 re-raise
    #   finally → endTrace + loadSessions
```

#### 关键设计决策

| 决策 | 理由 |
|---|---|
| 单气泡生命周期 | 打字占位、流式增量、最终回答复用同一 DOM 元素，消除多元素切换的状态管理复杂度 |
| 脱敏前移 | 先过滤再入记忆，防止敏感信息经上下文回流到下一轮 LLM |
| 中断落盘 | CancelledError 分支调用 _persist_session，用户停止不丢已生成内容 |
| finally endTrace | 无论成功/失败/中断都终止推理中状态，杜绝 UI 卡死 |
| create_task 持引用 | 后台任务存入 set + add_done_callback，防止 GC 提前回收 |
| ContextVar 身份 | user_id/session_id 通过 ContextVar 传递给工具层，避免改工具签名 |

#### token 统计

优先级：供应商真实 usage > 双侧估算

```
流式请求 payload 加 stream_options.include_usage=true
→ SSE 最后一个 chunk 携带 usage.total_tokens
→ 客户端解析后写入 llm_usage_sink ContextVar dict
→ 循环读取 sink["total"]，存在则用真实值
→ 不存在则 estimate_tokens(prompt) + estimate_tokens(completion)
```

估算公式：CJK 字符 × 0.7 + ASCII ÷ 4（对齐主流分词器实测均值）。

预算控制：累计用量达告警线（默认 80%）打 WARNING 日志，达上限按 TOKEN_BUDGET_HARD_STOP 决定终止或继续。

### LLM 客户端层

文件：`src/harness/llm/openai_compatible.py`

统一 OpenAI v1 兼容协议，任何供应商只需三个环境变量。

| 方法 | 用途 | 返回 |
|---|---|---|
| chat_async() | 非流式调用（工具修正重试轮） | LLMReply(content, total_tokens) |
| stream_chat_async() | 流式调用（主循环） | AsyncGenerator[str] |
| _post_with_retry() | 瞬时错误指数退避重试 | Response |

token 用量随 LLMReply 返回（非流式）或写入 ContextVar sink（流式），并发请求互不串号。

瞬时错误（超时/连接失败/5xx）自动指数退避重试 `LLM_MAX_RETRIES` 次；4xx 不重试直接抛出。

---

## 工具系统

所有工具继承 `BaseTool`，声明 `spec: ToolSpec` 即自动接入 LLM 调用协议。通过 ContextVar 获取请求上下文（user_id/session_id/budget）。

### 商品检索 knowledge_retrieval

两阶段架构：

```
第一阶段：向量召回候选池
  query_enricher.expand() → 生成变体（原查询 + 同义替换 + 预算注入）
  每个 variant 调 chroma.query(where=合并过滤条件) → 合并去重

第二阶段：精排
  BM25 关键字打分 + 向量距离排名 → RRF 融合
  意图词命中数作为一级排序键（拍照/游戏/小屏等）
  预算接近度作为二级加权
  可选：LLM Reranker 对 top-N 精排

自校正：best_distance > floor → 放宽价格约束二次召回
溯源：每条结果尾部附 [product_xxx]
库存：metadata.stock → 输出标注 库存N/紧张/售罄
```

过滤条件下推：价格区间、品类、在售状态编译为 ChromaDB where 子句，在向量检索层即过滤。

### 订单查询 order_query / order_list

SQLite 参数化查询 + 三层校验：

```
① 格式白名单：order_id 必须为 11-15 位数字
② 归属校验：WHERE user_id = current_user_id（ContextVar）
③ 枚举风控：连续 8 次未命中熔断 30 分钟（EnumerationGuard）
```

order_list 按 created_at DESC 返回最近 10 条本人订单。

### 物流查询 logistics_query

SQLite 查询 logistics 表 nodes_json 字段，格式化为编号+最新状态+逐节点轨迹。

枚举风控与订单查询共享 EnumerationGuard 实例池（按工具名隔离 key）。

### 售后管理 after_sale_apply / after_sale_query

申请流程：

```
用户提交 → 归属校验（owner==me?）→ 状态前置校验（已发货/配送中/已完成？）
→ 幂等检查（find_active_aftersale）→ 退款测算（calc_refund）→ SQLite INSERT
→ 返回售后单号 + 退款明细 + 时效说明
```

退款计算规则（calc_refund）：

| 场景 | 计算 |
|---|---|
| 整单退货 | 退商品小计 − 平台券全额（券不退）− 满减让利全额扣回 |
| 部分退货 | 退对应比例小计 − 券按件均摊（不退）；满减不扣回 |
| 极端优惠 | 结果钳制 ≥ ¥0 |

状态机唯一出口 `update_aftersale_status()`，非法流转抛 ValueError：

```
待审核 ──→ 已通过 ──→ 已完成
   │                    ↑
   └──→ 已拒绝          │
                        └── 已取消
```

每次流转记录 {ts, from, to, by} 到 history_json。

### 政策检索 policy_query

10 条官方政策条款存于 `data/policies.json`，BM25 关键字打分取 top-2。

未命中时返回明确告知 + 引导转人工指令，绝不编造条款。

### 转人工 transfer_human

创建工单记录写入 `data/tickets/tickets_YYYYMMDD.jsonl`，字段含工单号/会话/用户/原因/关联订单。返回确认话术含工单号。

### 计算器 calculator

AST 白名单求值器（`ast.NodeVisitor` 子类）：

- 允许节点：Expression / Constant / BinOp(+,-,*,/,//,%,**) / UnaryOp(+,-)
- 幂运算限制：底数 ≤1e6 且指数 ≤1000，结果 ≤1e308 且有限
- 拒绝一切 Call/Name/Attribute 节点（无函数调用、无变量访问）

### 子任务分发 subtask_dispatch

每个子任务独立 Registry + ShortTermMemory，支持同一工具多次调用。

禁止递归调用 subtask_dispatch 自身（运行期双重拦截）。子任务工具输出过护栏脱敏。

---

## 记忆体系

### 工作记忆 working_memory

```python
class WorkingMemory(BaseModel):
    budget_amount: float | None    # 预算金额
    budget_category: str | None   # 品类
    budget_turn: int | None       # 设定轮次
    order_ids: list[str]           # 提及的订单号（cap 10）
    tracking_nos: list[str]        # 物流单号（cap 10）
    recent_topics: list[str]       # 近期话题（cap 5）
    awaiting_slot: str | None      # 澄清等待项
    updated_turn: int              # 最后更新轮次
```

抽取规则（确定性正则，零 LLM 开销）：
- 预算：区分「明确预算」（预算3000/3999的手机/3k/2000预算）与「临时上限」（5000以下不覆盖既有预算）；用户改口自动更新并记录轮次
- 订单号：`20\d{9}` 11 位数字模式
- 物流号：`(SF|YT|ZTO|STO|JD|EMS)\d{9,12}`
- prompt_block() 渲染为「状态尾注」消息（对话末尾、本轮输入之前），含槽位与关键事实
- 轮末抽取**预筛**（_should_extract_turn）：命中硬实体信号（订单/物流/金额/预算正则）
  或意图/偏好关键词（喜欢/想要/推荐…）或输入较长（≥40 字）才调 LLM 抽取——
  纯寒暄/无信息轮直接跳过，省调用与限流配额
- 抽取路由**小模型优先**（`cheap_llm`，未配置自动回落主模型），经 cheap_semaphore 限并发
  （防瞬时打满小模型限流触发 429 风暴）；结果合并进 important_facts 并结构化写入长期记忆
- 更新语义：状态/进度等可变关系同实体同关系 → WM 原地替换；长期库一事实一记录（entity_key 元数据），写时替换 + 读时保鲜（跨会话只留最新）
- 垃圾防线：抽取为空/寒暄输入/失败轮 → 跳过长期写入；仅 LLM 异常时写确定性兜底文档
- 压缩事件时整块烘入冻结章节后 reset_for_new_cycle() 清零开新周期

### 短期记忆与章节压缩（v0.7.4 LSM 式）

ShortTermMemory 只追加设计：
- `get_context()` / `all_messages()`：纯 list 追加，无滑动淘汰（淘汰会打穿 KV cache 前缀）
- `split_for_compression(keep)` / `trim_to(recent)`：显式压缩接口

章节压缩流程（loop._persist_session）：
```
单会话消息估算 token >= CONTEXT_WINDOW_TOKENS × CONTEXT_COMPRESS_RATIO?
  ├─ 是 → 本周期旧消息调 LLM 章节摘要（不合并旧章节，零级联损耗）
  │       成功 → 组装冻结章节【第N阶段】摘要+WM快照 → chapters.append()
  │            → WM reset_for_new_cycle() 清零开新周期
  │            → 落盘 {messages: 最近 keep_recent 条, chapters: 追加后全量}
  │       失败 → 落盘完整历史（降级保数据）
  └─ 否 → 直接落盘全部
```
触发阈值按「相对模型窗口」估算（`estimate_tokens` 启发式，非精确计数）：
换模型只需调整 `CONTEXT_WINDOW_TOKENS`（所用模型的上下文窗口）即可适配其触发时机。

上下文组装 = [system 人设+工具] + [system 章节×k] + [历史(只追加)] + [system 状态尾注=当期WM+跨会话召回] + [本轮输入]。
稳定期纯追加 → KV cache 零失效；压缩事件仅历史区位移一次。

### 长期记忆 long_term_memory

ChromaDB collection `agent_long_term_memory`（cosine 空间），按 user_id 过滤 + 距离阈值截断。

---

## 安全护栏

### 输入校验与注入防护

InputValidator：空值 / 4096 上限 / 控制字符 `[\x00-\x08\x0b\x0c\x0e-\x1f]`。

InjectionGuard：5 组中英文注入特征正则匹配，命中即拒绝并审计留痕。可通过 `PROMPT_INJECTION_BLOCK=false` 关闭。

### 输出脱敏与合规过滤

OutputFilter：5 组敏感信息模式掩码为 ***（身份证18位含X/15位/手机号/银行卡/API Key）。先于入库执行。

ComplianceFilter：绝对化承诺检测（百分百能退/保证到账等）追加「以官方政策为准」提示；违禁词 * 替换。

### 限流与审计

RateLimiter：滑动窗口 + per-session_id 隔离桶。达到上限返回剩余等待秒数。

AuditLogger：JSONL 追加写入，content_preview 写入前过 mask_sensitive() 掩码。按 AUDIT_ROTATE_MB 大小轮转为 .N.jsonl 序号文件。拦截事件由流水线回调 record_blocked() 补记。

---

## 存储层

### SQLite 业务库

WAL 模式，六张表：

| 表 | 用途 |
|---|---|
| products | 商品主数据（名称/品类/品牌/价格/描述/规格/标签/状态/库存） |
| orders | 订单（归属用户/商品关联/优惠字段/状态/物流关联） |
| logistics | 物流轨迹（tracking_no → nodes_json） |
| aftersale | 售后单（状态机/history_json/退款金额） |
| sessions | 会话元数据（title/summary/working_memory/traces） |
| session_messages | 会话消息（session_id+seq 联合主键） |

首连按 db_path 自动建表 + 缺失列 ALTER 迁移。短连接 + WAL 读写不互斥。

### 向量库同步

vector_sync.py 提供：

| 函数 | 说明 |
|---|---|
| render_product_doc() | 唯一文档渲染器，seed/管理API/重建共用 |
| product_metadata() | 含 status/stock 的 metadata 构建 |
| upsert_products() | 批量 upsert |
| delete_product() | 下架/删除时同步清除向量 |
| reindex_all(prune) | DB 全量重建 + 脏 id 对账清理 |

---

## 评测框架

运行方式与层级详见 [docs/USER_GUIDE.md](USER_GUIDE.md) 评测章节。

关键设计：
- retrieval 层 GT 由 SQLite 结构化字段运行时计算（非手工标注，不怕目录变更漂移）
- budget 层超预算为零容忍硬断言
- robustness 层覆盖幂炸弹秒拒 / 代码注入 / PII 掩码 / 限流隔离 / 路径穿越
- routing 层 spy registry 记录真实 LLM 的工具选择
