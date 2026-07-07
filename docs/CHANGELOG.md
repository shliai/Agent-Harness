# Agent Harness 更新日志

## [v0.2.0] - 2026-07-07

对比首次提交（v0.1.0，commit `25bf426 init`）后的全部代码改动。本次版本聚焦四件事：**长期记忆落地**、**全链路异步化**、**前端体验与稳定性修复**、**检索与数据质量治理**。

### 新增

#### 长期记忆模块（`src/harness/memory/long_term.py`）
- 基于 ChromaDB 独立 collection `agent_long_term_memory`，复用 BGE 嵌入模型
- 跨 session_id 语义检索历史对话，让 Agent 能"记得"过往交互
- 受 `LONG_TERM_ENABLED` 开关控制，关闭时完全不加载 ChromaDB，零开销
- **写入策略**：仅在 ReAct 循环正常完成后写入一条（user_input + 最终 answer），失败/拦截不写入避免噪声
- **检索策略**：每轮对话开始时用当前用户输入做语义查询，Top-K 相关历史注入 system prompt 的「相关历史记忆」段落
- **安全降级**：初始化失败自动 `enabled=False`，所有操作 try/except 兜底，**永远不会**因长期记忆故障影响主流程
- 新增配置项 `LONG_TERM_TOP_K`（默认 3），并在 `config.py` 中声明

#### BGE 嵌入模型共享与预热（`src/harness/memory/embeddings.py`）
- 全局单例 BGE 嵌入模型管理器
  - `get_embed_fn()`：懒加载 + 缓存，整个进程只加载一次 BGE 模型（约 21 秒）
  - `warmup()`：主动触发模型加载，供启动时调用
- **共享实例**：`KnowledgeRetrievalTool` 和 `LongTermMemory` 共用同一 embedding function，避免重复加载（省 21 秒）
- **启动预热**：`main.py` 启动时调用 `warmup()`，把模型加载从请求路径移到服务启动阶段
- 实测效果：首次请求耗时从 **48.63 秒 → 3.97 秒**（12.3 倍提升）

#### Web 层 Agent 启动预热（`src/harness/web/api.py`）
- 新增 `_build_agent()` 工厂函数，抽出 Agent 构造逻辑
- 新增 `warmup_agent()`：服务启动时主动初始化 Agent，工具注册和 ChromaDB 初始化提前到启动阶段，首次请求不再等待

#### 前端停止生成功能（`src/harness/web/static/index.html`）
- 新增「停止」按钮，支持手动中断正在进行的 SSE 流
- 引入 `AbortController`，90 秒超时自动取消，避免 LLM 无响应时一直转圈

### 优化

#### 全链路异步化
针对高并发场景下事件循环被阻塞导致 SSE 推送卡顿、响应变慢的问题，对 LLM 客户端和长期记忆模块做了异步化改造。

- **`AbstractLLMClient`** 新增 `chat_async()` 和 `stream_chat_async()` 两个抽象方法
- **`OpenAICompatibleClient`** 实现 async 版本：
  - 引入 `httpx.AsyncClient` 连接池复用，减少 TCP/TLS 握手开销
  - `chat_async()` / `stream_chat_async()` 内部使用 `await` + `async with`，不再阻塞事件循环
  - 保留同步版本 `chat()` / `stream_chat()` 以兼容同步调用场景
- **`ReActLoop`** 4 处 `llm.chat()` 改为 `await llm.chat_async()`
- **`SubTaskDispatchTool`** 2 处 `llm.chat()` 改为 `await llm.chat_async()`
- **`LongTermMemory.add()` / `search()`** 改为 `async def`
  - 内部使用 `asyncio.to_thread()` 把同步的 ChromaDB 调用（含 BGE 编码）放到线程池执行
  - 避免 BGE 编码（~200-500ms）和 ChromaDB 查询（~100-300ms）阻塞事件循环
- **`ReActLoop`** 中长期记忆写入改为 `asyncio.create_task()` 后台执行
  - 响应不再等待长期记忆写入完成，直接返回
  - 写入失败仍由 try/except 兜底，不影响主流程

#### 直接回答也推送 step 事件
- 之前 LLM 不调工具直接回答时，不推送 step 事件，前端看不到思考过程
- 现在 `tool_call is None` 分支也 `yield step_payload`，让前端能看到 LLM 的 thought

#### 默认参数调优（`.env.example`）
- `TEMPERATURE`：0.7 → 0.3（降低随机性，加速推理）
- `MAX_TOKENS`：4096 → 2048
- `MAX_ITERATIONS`：10 → 6（限制最坏情况耗时）
- `TOOL_MAX_RETRIES`：2 → 1

#### 检索排序：预算场景引入价格接近度加权（`src/harness/tools/knowledge_retrieval.py`）
- 之前纯靠语义 hybrid_score 排序，用户问"3999的手机"时 BGE 把"性价比手机"语义相近的低价款排前面，正好匹配预算的小米14 ¥3999 被挤出 top-K
- 新增预算接近度加权：检测到 `price_max` 时，**语义分占 40%，价格接近度（`price / price_max`）占 60%**，让接近预算上限的商品排前面
- 实测效果：查询"3999的手机"，小米14 ¥3999 从被挤出 top-5 → 排到第一

#### 商品数据规范化（`data/products.json`）
- **tags 四维度规范**：性能档位（旗舰/次旗舰/中端/入门）+ 使用场景（高性能/游戏/商务/拍照等）+ 特殊属性（小屏/4K/降噪等）+ 生态（iOS/安卓/鸿蒙等）
- **修正错误标签**：
  - Redmi K70 Pro：`["性价比"]` → `["次旗舰","高性能","游戏","性价比"]`（骁龙8 Gen3 旗舰芯片，原标签严重低估）
  - 小米14：补"高性能"标签（原漏标，导致"高性能手机"检索不到）
  - iQOO Z9 Turbo：补"高性能"标签（骁龙8s Gen3 是次旗舰性能）
  - iPhone 15：`["中端"]` → `["次旗舰"]`（A16 是次旗舰芯片）
- **新增 description 字段**：每个商品一句话自然语言描述，含性能/场景关键词，让 BGE 语义检索匹配度更高（自然语言文本对 BGE 检索效果远优于 JSON 字符串）

#### seed 脚本 document 改为自然语言拼接（`scripts/seed_products.py`）
- 旧：`document = json.dumps(product)`（JSON 字符串，BGE 检索效果差）
- 新：`document = f"{name} | {brand}{category} | ¥{price} | {description} | {specs_str} | 标签：{tags}"`（自然语言文本，BGE 检索效果显著提升）
- "高性能手机"查询能匹配到 description 里的"性能强劲"等关键词，不再依赖纯标签匹配

### 修复

#### 前端异常处理与状态恢复（`src/harness/web/static/index.html`）
- 修复错误处理变量作用域问题：`resp` / `reader` 提升到 try 块外声明，`finally` 中可安全访问
- 区分错误类型：用户主动取消（AbortError）、网络中断（Failed to fetch）、服务端错误，给出不同提示
- 错误时保留已收到的部分推理轨迹，便于排障
- `finally` 中确保 `reader.releaseLock()` 释放资源，避免连接泄漏
- 切换/删除会话时主动取消正在进行的流，避免状态错乱

#### 浏览器缓存导致前端改动不生效（`src/harness/web/api.py`）
- `/` 路由响应增加 `Cache-Control: no-cache, no-store, must-revalidate` 头
- 浏览器每次都发请求验证，确保拿到最新版本的 `index.html`

#### 工具结果展示截断过短（`src/harness/web/static/index.html`）
- 推理步骤中的工具执行结果展示长度从 300 → 600 字符，避免关键信息被截断

#### 检索工具不识别"X的手机"精确预算表达（`src/harness/tools/knowledge_retrieval.py`）
- 之前 `_extract_filters` 只识别"X 以内/X-Y/X 以上"三种价格表达，遇到"3999的手机"完全不设价格过滤，所有价位手机都被检索出来
- 新增正则 `r"(?:预算\s*(\d+)|(\d+)\s*元?\s*的|\b(\d+)\s*元?\s*预算)"` 识别"X的手机/预算X/X预算"等表达，转化为 `price_max=X`
- 加 100-99999 元合理性校验，避免误匹配型号数字（如"iPhone 15"的 15）

#### LLM 推荐超预算商品（`src/harness/core/loop.py`）
- 之前 system prompt 缺乏预算约束规则，LLM 自行脑补出"略超预算但值得考虑"的逻辑，推荐 ¥4999 商品给预算 3999 的用户
- 新增「预算约束」段落：
  - 严格视为预算上限，禁止推荐超预算商品
  - **预算多轮持续生效**：之前对话提过预算，后续推荐同品类商品仍需遵守，除非用户明确变更/取消
  - **调用工具时合并预算到 query**（如用户之前说 3999，现在问"高性能手机"，应传 query="高性能手机 3999元以内"）
  - 优先推荐接近预算上限的款，预算内按价格从高到低排序
  - 预算内无匹配时明确告知，再推荐最接近的 2-3 款并标注"略超预算"

#### 知识库冷数据缺口（`data/products.json`）
- 新增 6 款 3000 元以内拍照手机：Redmi Note 13 Pro（¥1399）、荣耀 X50（¥1499）、realme GT Neo6 SE（¥1799）、iQOO Z9 Turbo（¥1999）、OPPO K12（¥2199）、vivo Y200 GT（¥2499）
- 解决"预算 3000 元以内拍照手机"类查询因无匹配商品导致 LLM 反复重试、触发 `MaxIterationsExceeded` 的问题

#### seed 脚本重复入库（`scripts/seed_products.py`）
- 原本用 `collection.add` 追加模式，id 基于当前 `exist_count` 生成（每次运行 id 不同），多次运行会重复入库且无法去重（实测 56 条商品被 seed 成 106 条）
- 改为 `collection.upsert` + 稳定确定性 id（`product_000`~`product_055`），幂等可重复运行
- products.json 修改后直接重跑即可同步，无残留

### 测试

- `MockLLMClient` / `StatefulMockLLM` 同步实现新增 async 方法（`chat_async` / `stream_chat_async`）
- 新增 `tests/unit/test_long_term_memory.py`：19 个用例覆盖禁用态、启用态、初始化失败降级、ReActLoop 集成（注入/写入/失败不写入）
  - mock 目标为 `get_embed_fn`（适配共享单例）
  - 全部 add/search 用例改为 `async def` + `@pytest.mark.asyncio`
- `test_config.py` 修正 `max_iterations` 默认值断言（10 → 6）
- 全量 56 个测试通过（19 个长期记忆用例 + 37 个原有用例）

### 文档

- 同步更新 README、ARCHITECTURE、docs/README
- ARCHITECTURE 中 LLM 层、记忆层、API 数据流均补充异步化说明
- README 修正"长期(ChromaDB)"的描述（之前是预测性描述，现已真实落地）

### 检索效果对比

| 查询 | 修复前 top-5 | 修复后 top-5 |
|---|---|---|
| "3999的手机" | iQOO Z9 Turbo ¥1999 / vivo Y200 GT ¥2499 / OPPO K12 ¥2199 / 荣耀 X50 ¥1499 / realme GT Neo6 SE ¥1799（**小米14 ¥3999 被挤出**） | **小米14 ¥3999** / Redmi K70 Pro ¥3299 / vivo Y200 GT ¥2499 / OPPO K12 ¥2199 / iQOO Z9 Turbo ¥1999 |
| "3000元以内拍照手机" | 混入超预算商品 | 全部 ≤3000 元，且按价格接近度排序 |
| "高性能手机" | 小米14 被挤出（漏标"高性能"标签） | 含 iQOO Z9 Turbo、Samsung S24 Ultra、Redmi K70 Pro、OPPO Find X7 等高性能款 |

### 代码改动统计

```
 .env.example                             | 12 +++--
 README.md                                | 16 +++++--
 data/products.json                       | 70 ++++++++++++++++++++-----
 docs/ARCHITECTURE.md                     | 77 +++++++++++++++++++++++++------
 docs/CHANGELOG.md                        | 130 +++++++++++++++++++++++++++++++++++++++++++++++
 docs/README.md                           |  6 ++-
 scripts/seed_products.py                 | 30 ++++++++--
 src/harness/config.py                    | 1 +
 src/harness/core/agent.py                | 3 ++
 src/harness/core/loop.py                 | 60 ++++++++++++++++++++++--
 src/harness/llm/base.py                  | 10 +++-
 src/harness/llm/openai_compatible.py     | 75 ++++++++++++++++++++++++++++--
 src/harness/main.py                      | 4 ++
 src/harness/memory/__init__.py           | 5 ++
 src/harness/memory/embeddings.py         | 43 +++++++++++++++++ (新增)
 src/harness/memory/long_term.py          | 154 ++++++++++++++++++++++++++++++++++++++++++++++++++ (新增)
 src/harness/tools/knowledge_retrieval.py | 60 ++++++++++++++++++++++--
 src/harness/tools/subtask_dispatch.py    | 4 +-
 src/harness/web/api.py                   | 60 +++++++++++++++++-------
 src/harness/web/static/index.html        | 75 ++++++++++++++++++++++++++----
 tests/conftest.py                        | 10 +++-
 tests/integration/test_agent_loop.py     | 8 +++-
 tests/unit/test_config.py                | 2 +-
 tests/unit/test_long_term_memory.py      | 337 +++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++ (新增)
 25 files changed, 580 insertions(+), 90 deletions(-) (+ 2 个新文件)
```

## [v0.1.0] - 2026-07-01

### 初始版本发布

- 基础 ReAct 循环引擎实现（`src/harness/core/loop.py`）
- 工具系统完整实现（5 个工具：knowledge_retrieval, order_query, logistics_query, calculator, subtask_dispatch）
- 记忆系统实现（ShortTermMemory + ConversationHistory + SessionManager）
- 安全护栏实现（InputValidator, OutputFilter, RateLimiter, AuditLogger）
- Web API 完整实现（FastAPI, 9 个端点, SSE 流式响应）
- 前端聊天界面（暗色主题, 3 栏布局, ReAct 步骤可视化）
- 测试覆盖：37 个（29 个单元测试 + 8 个集成测试）
- 构建系统：hatchling, Python >= 3.11
