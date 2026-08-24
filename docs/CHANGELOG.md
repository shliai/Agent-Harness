# Agent Harness 更新日志

## [v0.7.3] - 2026-08-24

**Markdown 渲染器重写 + 流式渲染防跳动专项**。159 个测试全部通过；JS 语法校验通过。

### Markdown 渲染器重写
- 新增：**表格**（带边框样式）、**块引用**（左侧竖线）、**有序列表**、**分割线**
- 代码块改为占位符机制：先提取再转义，杜绝代码内容被行内格式误伤
- 未闭合 ``` 围栏流式期间即时渲染为代码框，消除「纯文本→代码框」剧跳

### 流式渲染防跳动（7 处）
- delta 节流 80ms 批量重绘，替代每 token 全量 `md()` 解析 + innerHTML 替换
- `down()` 滚动用 rAF 合并，一帧最多一次，消除强制同步重排
- 光标零宽化（`width:0;border-left` + 负 margin）且流式期间常驻——出现/消失不再改变行尾换行位置
- `step` 事件不再清空气泡，改为折叠为摘要行，修复多轮工具调用时内容消失重现
- `endTrace` 无步骤时平滑折叠动画后移除，修复「回答完成瞬间下方内容上跳」
- `.chat` 增加 `overflow-anchor:auto` 与 `scrollbar-gutter:stable`，滚动条出现/消失不再全文回流
- spinner 改 `visibility:hidden` 占位，胶囊内文字不左移

### 缺陷修复
- **恢复被误删的 `addMsg()` / `wmChip()` 函数**——此前点击旧会话必报「加载会话失败」（ReferenceError 被 catch 吞掉）
- 补齐流结束兜底：未收到 result 事件时强制最终 Markdown 渲染并移除光标，光标不再永久闪烁
- `paint()` 移除 `includes("```")` 粗暴判断（含代码块即全文退化为纯文本），始终走 md() 渲染，异常时降级文本

## [v0.7.2] - 2026-08-23

**前端整体重构（黑白双主题 / 全响应式）+ 运营功能补全**。159 个测试全部通过；真机验证通过（批量删除 / 管理鉴权 / 400 商品分类加载）。

### 前端重写（响应式重构）
- 布局改为 flex 浮层方案：侧栏/抽屉全部 overlay 化，`100dvh` + `clamp()` 流式缩放，修复小窗/缩放比例失真
- **黑白双色主题**：顶栏 ◑ 切换（浅色跟随系统），持久化偏好；移除全部蓝紫渐变
- 流式气泡改为**单元素生命周期**（打字→增量→最终同一节点），从结构上消灭重复头像与残留光标类缺陷
- 中止/网络异常路径统一清理临时内容

### 会话批量删除
- 后端新增 `POST /api/sessions/batch-delete`（归属校验：仅本人名下或无归属会话可删，越权静默跳过）
- 前端侧栏复选框多选 → 批量条显示已选数 → 删除所选/取消；当前会话被删自动回到新对话

### 商品库完整管理（运营面板）
- **全量展示**（分页 15/页）+ 搜索（名称/编号）+ 类别筛选 + 状态筛选
- 行内**编辑**：名称/品类/品牌/价格/描述/标签/状态全字段修改，保存即同步向量库
- 上下架切换、删除（含向量索引）、新增上架、一键重建向量索引

### 管理面板 UX
- Token 输入框带默认值提示，输入后自动生效并刷新列表
- 双主题按钮去重；管理入口合并为单抽屉 Tab（商品库/售后审核）

## [v0.7.1] - 2026-08-23

**全项目 debug 与稳定性加固轮**。159 个测试全部通过；静态扫描（未使用导入/死赋值/调试残留/冗余文件）归零。

### 边界情况修复（用户可见）
- **坏 JSON 连续失败降级**：ACTION 解析重试耗尽后不再把原始残片当回答输出，回滚流式文本并返回友好提示（含转人工引导）
- **空回复兜底**：LLM 返回空内容时生成兜底话术，避免前端渲染空气泡
- **跨用户写保护**：已归属会话拒绝其他 user_id 追加内容（403），与读侧 403 对称；chat 接口写入前轻量查询归属
- **前端中止残留**：请求异常/停止生成时清理临时流式气泡，修复光标无限闪烁

### 检索排序语义修正
- 意图匹配从「加成分数」改为「分级排序键」：(意图命中数, 相关度) 二级排序——强意图优先满足的同时保留同命中档内的相关度区分，消除加成数值掩盖相关度差异的问题

### 稳定性改进
- LLM 重试最后一次尝试不再执行无意义退避等待
- Reranker：LLM 漏排候选按原序补回，不丢失商品
- 变体多路召回单路失败自动跳过，不拖垮整体检索
- 会话详情接口归属校验与数据返回合并为单次读取

### 清理
- BOM 文件修复 ×1；未使用导入 ×10（conftest/integration/unit 多处）；死赋值与退役模块引用清零；冗余文件零残留

## [v0.7.0] - 2026-08-23

**Agentic RAG 检索升级 + 数据规模扩容 + 生产化加固 + 运维手册**。159 个测试全部通过；400 商品目录下检索评测 13/13 满分。

### Agentic RAG（知识检索路径升级）
- **查询改写与 MQE-lite**（`tools/query_enricher.py`）：领域同义词表生成变体查询多路召回；工作记忆预算/品类槽位确定性注入 query——替代依赖 prompt 提醒的旧方式
- **自校正二次召回**：向量距离超阈值视为召回不相关，自动去掉价格约束放宽重查一次并合并重排，输出标注「已放宽价格条件」
- **意图词加成排序**：查询含拍照/游戏/小屏等强意图词时，tags 命中候选获得 hybrid 加成——修复大目录下「贴预算但不匹配意图」商品挤占头部（R01 recall 0.20→1.00、R11 0.00→1.00）
- **LLM as Reranker**（`llm/reranker.py`）：RRF 粗排 top-20 交 LLM 精排 top-5；解析失败/上游限流自动回退 RRF 序并进入 60 秒冷却；`RERANK_ENABLED` 开关
- **引用溯源输出**：商品行尾附 [product_xxx]、政策附 [POL-xxx]，system prompt 强制推荐引用编号

### 数据规模扩容（策划期固化）
- 商品目录 62 → **400 条**（品牌家族 × SKU 版本系统性扩充，价格阶梯符合行情），冻结为 `data/seed/products.json`
- 订单 **260 单 / 物流单号精确 200 个**（`data/seed/orders.json`、logistics.json）；demo_user 全状态覆盖、30 用户分布
- `storage/seeds.py` 统一装载器；init_db 支持 `--reset --reindex`；sample_data.py 运行时生成器退役删除

### 生产化加固
- **Prompt 注入防护护栏**（InjectionGuard）：中英文指令注入特征拦截，可经 `PROMPT_INJECTION_BLOCK` 关闭，命中即审计留痕
- **参数格式白名单**：order_id（11-15 位数字）/物流单号（承运商前缀+数字）在工具入口校验，畸形参数计入枚举风控
- **上游瞬时错误重试**：超时/连接错误/5xx 指数退避重试 `LLM_MAX_RETRIES` 次，4xx 不重试
- **会话 token 预算**：累计用量达告警线（默认 80%）打 `[ALERT][BUDGET]` 日志；达上限按 `TOKEN_BUDGET_HARD_STOP` 决定优雅终止或继续告警

### 运维能力
- `/metrics/prometheus` 文本格式指标端点（LLM 调用/token/耗时/分工具计数/uptime）
- 灰度可观测：全响应携带 `X-Release-Channel` 头（RELEASE_CHANNEL 配置）
- **docs/OPERATIONS.md** 新增：日志聚合方案（stdout→Filebeat→Loki/ELK）、抓取配置与告警规则示例、灰度发布路径、密钥两级解析（OPENAI_API_KEY_FILE 支持挂载 Secret）、数据生命周期表、故障速查

### 测试
新增 9 个用例（注入防护/查询改写/token 预算字段/审计轮转），总计 **159 passed**。

## [v0.6.0] - 2026-08-23

**售后业务双侧闭环（用户侧申请 + 商家侧审核）与退款资金测算**。150 个测试全部通过；另完成全项目 debug 专项（修复 OutputFilter 失效、工单静默丢失、clear_session 越权、schema 缓存错位等 4 个真实缺陷）。

### 会话归属校验（P0 安全修复）
- sessions.user_id 落库，首次写入后不可篡改
- GET/PUT/DELETE /api/sessions/{id} 与 clear 接口：请求携带 X-User-Id 且与会话归属不一致时返回 403；匿名请求保持兼容
- 列表接口按 X-User-Id 过滤（名下 + 无归属遗留会话）
- 审计日志 content_preview 写入前统一 PII 掩码（复用 mask_sensitive）

### 商家审核侧
- `tools/aftersale_admin.py`：approve / reject / complete 业务函数，语义校验（存在性、合法流转、驳回必填原因）+ 操作留痕（operator/note 进 history）
- Admin 端点四条：GET /api/admin/aftersales?status= 、{as_id}/approve | reject | complete（均挂 require_admin + 管理限流）
- 前端管理抽屉新增「售后审核」面板：状态筛选、通过/拒绝（必填原因）/完成打款操作

### 退款资金测算
- `calc_refund`：平台券按件均摊且抵扣部分不退；满减让利整单退时全额扣回、部分退不扣回；结果钳制非负
- orders 表新增 discount_coupon / discount_promo 两列（存量库自动 ALTER 迁移）；拟真数据按 35%/20% 分布生成优惠
- after_sale_apply 支持 qty 部分退，申请即返回逐项退款测算明细并随售后单落库

### 全项目 debug 专项修复
1. OutputFilter 敏感模式元组被打扁导致掩码整体失效（回归测试盲区，已由审计断言覆盖）
2. TransferHumanTool 工单写入 NameError 被 except 吞掉造成静默丢数据
3. clear_session 未做会话归属校验
4. schema 初始化缓存不感知 db_path 切换（多路径场景建表缺失）
另清理未使用导入 7 处、退役 SessionManager/sync LLM 接口/set_llm 死代码及冗余文件。

## [v0.5.0] - 2026-08-23

**架构一致性收尾 + 体验质变 + 可交付形态**。138 个测试全部通过；五层评测 28/28 全 PASS（retrieval/budget/robustness/memory + 扩容后 routing 待 --live）。

### 会话历史进 SQLite
- sessions + session_messages 两表承接全部会话状态（messages/summary/working_memory/traces/title）
- 事务原子性替代临时文件方案；并发由 SQLite WAL 兜底；过期清理基于 updated_at（SESSION_CLEANUP_HOURS）
- **旧版 JSON 会话文件首次使用自动迁移**，零数据丢失升级
- 顺带修复：schema 初始化缓存按 db_path 路径隔离（多 tmp 库测试暴露）

### LLM token 级真流式
- ReAct 主循环改用 stream_chat_async，增量以 SSE `delta` 事件即时推送——首字延迟从整轮生成结束降到 1-2s
- 新事件语义：`delta_reset`（该轮实为工具规划，前端回滚临时文本）/ `answer_replace`（脱敏改变了原文，整体覆盖）
- 前端：增量 Markdown 渲染、规划轮自动回滚进思考时间线、光标动效

### 商品管理抽屉（前端）
- 管理面板：Token 鉴权输入持久化、商品列表（在售/下架状态切换）、删除（联动向量库）、新增上架表单、一键重建向量索引

### 评测体系扩展
- golden_set 扩容至 36 条（routing 6→10、retrieval 8→13、budget +1），覆盖政策路由/订单列表/售后意图/投诉转人工
- **新增长期记忆检索层**：种子对话注入临时 collection，验证语义命中与 MRR（当前 4/4）
- `scripts/export_badcase.py`：从审计 blocked 事件 + 失败工具轨迹挖掘 badcase 候选，人工评审后回流 golden set

### 运维与安全
- 审计日志按大小轮转（AUDIT_ROTATE_MB，默认 16MB → .N.jsonl 序号）
- 管理 API 独立限流（30 次/分钟/Token，超限 429）

### 检索能力补强（评测驱动）
- 中文数量词归一化：「万元以上」→10000 元、「1万5」→15000、「2万8以上」→28000
- 修复评测事实源错位：eval 商品 GT 改以 SQLite 为准（与向量库一致），products.json 同步再导出

### 可交付形态
- Dockerfile（python3.11-slim + 内置 BGE 模型 + HEALTHCHECK + 启动自动建库）
- docker-compose.yml（./data 卷持久化 + env_file）
- GitHub Actions CI：ruff 关键规则门禁 / pytest / main 分支镜像构建验证

## [v0.4.0] - 2026-08-23

**去 mock 化：SQLite 生产级数据层 + 商品维护体系 + LLM 接入统一**。136 个测试全部通过；离线端到端验证通过（下架过滤 / 恢复联动 / 真库订单查询）。

### SQLite 业务库（`storage/db.py`）
- 订单 / 物流轨迹 / 商品 / 售后四张业务表落库 `data/harness.db`，WAL 模式读写不互斥，MOCK 字典全部退役
- 首连自动建表；短连接模式天然线程安全；工具层查询统一走线程池不阻塞事件循环

### 拟真数据（`storage/sample_data.py`）
- 62 条手工策划的 2026 年 3C 目录（手机/笔记本/平板/耳机/穿戴/配件），价格贴近真实行情
- 固定种子确定性生成 130+ 订单：多用户、状态分布贴近真实、时间分布近 120 天、发货单自动带物流轨迹；demo_user 四种关键状态全覆盖

### 初始化与对账（`scripts/init_db.py` + `storage/vector_sync.py`)
- 一条命令建库填充：`python scripts/init_db.py [--reindex]`
- **文档渲染器唯一化**：seed/管理 API/重索引共用同一渲染函数，格式永不漂移
- 对账式重建 reindex_all(prune=True)：DB 为事实源，向量库脏 id 自动清理
- **下架即删除**：管理端删除商品时 DB 与向量索引同步移除；检索层恒定附带在售状态过滤双保险

### 商品维护 API（管理员鉴权 X-Admin-Token）
- `POST/PUT/DELETE/GET /api/admin/products`：增删改查实时联动向量库
- `POST /api/admin/products/reindex`：手动全量对账重建

### LLM 接入统一
- 移除 zhipu 专属分支，所有供应商统一走 OpenAI v1 兼容三元组配置（OPENAI_API_URL / OPENAI_API_KEY / OPENAI_MODEL）
- OpenAI / 智谱 / DeepSeek / 通义 / 本地 vLLM 均只需改三个环境变量

### 修复
- subtask 重试路径引用未定义 system_prompt 的 NameError（测试暴露）
- init_db 嵌套连接写锁死锁（改为同事务 executemany）

## [v0.3.2] - 2026-08-23

**业务闭环 + 安全加固**。新增 10 个测试（总计 130 个全部通过）；真机验证通过：订单查询 / 待发货转人工 / 售后申请落盘三条真实 LLM 链路。

### 业务闭环（写操作从零到一）
- `after_sale_apply` / `after_sale_query`：退货换货申请与进度查询；状态机唯一流转出口（待审核→已通过→已完成/已拒绝），非法跳转抛错；订单归属 + 状态前置校验；同订单进行中售后幂等防重复
- `policy_query`：结构化政策库（10 条官方条款），政策类问题强制查库回答，未命中明确告知并引导转人工——堵住「LLM 编造政策」的资损口子
- `transfer_human`：转人工工单落盘 JSONL（工单号/会话/用户/原因/关联订单）
- `order_list`：按当前用户列订单，解决「不记得单号」入口缺失

### 安全加固
- 订单归属校验：50 条 mock 订单注入 owner 字段，非本人订单拒绝查询/售后
- 枚举风控：同会话连续 8 次查询未命中即熔断 30 分钟，命中清零——封死遍历拖库路径
- 输出合规护栏 ComplianceFilter：绝对化承诺（百分百能退/保证到账等）自动追加「以官方政策与人工审核为准」提示并告警留痕；输出违禁词 * 替换

### 流程闭环修复
- **流式中断不再丢数据**：客户端断开/停止生成时 CancelledError 分支把部分对话与工作记忆落盘
- **历史推理轨迹持久化**：每轮 steps 存入会话文件 traces（保留最近 8 轮），前端回放可展开查看当时的 Thought/Action/Observation
- **同会话并发锁**：进程内 per-session Lock，并发请求互不覆盖会话文件
- **澄清式多轮**：WorkingMemory.awaiting_slot 记录反问等待项，用户回复后自动清除，配合系统提示协议防止连环追问

### 其他
- 轻量用户体系：ChatRequest.user_id 贯穿订单归属 / 售后 / 工单 / 长期记忆隔离（默认 demo_user）
- LLM 客户端超时可配置化预留；Agent.run 透传 user_id

## [v0.3.1] - 2026-08-23

**记忆系统完善 + 上下文工程 + 四层评测框架**。新增 18 个测试（总计 106 个全部通过），离线评测 18/18 PASS。

### 工作记忆（新模块 `memory/working_memory.py`）
- 结构化任务状态槽位：预算（金额/品类/设定轮次）、订单号、物流单号、近期话题
- 确定性规则抽取，零 LLM 开销；跨轮持久注入 system prompt「任务状态」块
- 区分「明确预算」（预算3000 / 3999的手机 / 2000预算 / 3k）与临时价格上限（"5000以下"不覆盖长期预算）；用户改口自动更新并记录轮次
- 随会话状态原子落盘，多轮对话中预算不再因窗口滑动被遗忘

### 会话压缩（上下文工程）
- 超过 `CONTEXT_COMPRESS_THRESHOLD` 条时，把较旧对话用 LLM 压成 ≤500 字滚动摘要
- LLM 视角 = 系统提示 → 工具说明 → 历史摘要 → 工作记忆 → 长期记忆召回 → 最近 `KEEP_RECENT` 条
- 摘要必须保留预算/单号/结论/未决诉求；压缩失败降级保留完整历史，绝不丢数据
- ShortTermMemory 支持 track_full：LLM 只看窗口，落盘保留全量

### 四层评测框架（`scripts/eval.py` + `data/eval/golden_set.jsonl`）
- **retrieval**：Recall@5 / MRR / 价格与品类硬合规；ground truth 从 products.json 运行时计算
- **budget**：超预算零容忍 + top1 价格接近度软指标
- **routing**（--live）：spy registry 记录真实 LLM 的工具调用，断言期望命中
- **robustness**：幂炸弹秒拒 / 代码注入 / PII 脱敏 / 控制字符拦截 / per-key 限流 / 路径穿越
- 报告 JSON 落盘 `data/eval/report_*.json`

### 评测驱动的修复
- 「2000预算」「预算只有2500」等倒序预算表达此前不被识别，导致带预算查询漏掉价格过滤（B02 超预算 2 款 → 0）

## [v0.3.0] - 2026-08-23

全面审计后的**修复 + 加固 + 前端重写**版本。共修复 20+ 项缺陷（含 2 项严重级），新增 32 个回归测试（总计 88 个全部通过）。

### 严重修复
- **子任务分发控制流缺陷**（`tools/subtask_dispatch.py`）：原 `while...else + break` 结构导致子任务执行一次工具后即整体中断、结果为空 `{}`。重写为「LLM 决策 → 执行 → 观察 → 再决策」的清晰循环，多步子任务可完整跑通；子任务工具输出同样过护栏脱敏。
- **会话接口路径穿越漏洞**（`web/api.py`）：session_id 未校验直接拼文件路径，Windows 下 `%5C` 可读写删任意 `.json`。新增 `^[A-Za-z0-9_-]{1,64}$` 白名单校验，全部端点生效。

### 检索重写
- BM25 此前对中文完全失效（`str.split()` 无分词，实测得分恒为 0）。内置自包含分词器（ASCII 词 + CJK 二元组），检测到 jieba 时自动启用更精准分词。
- 融合算法从「批次内 max 归一化加权」改为 **RRF 排名融合**，消除分数尺度问题；价格/品类过滤下推向量 where 条件；新增 `RETRIEVAL_CANDIDATES` 两阶段候选池。
- 预算表达扩展：支持 `3k` / `3千` / `3000块`；移除必然失败的 `json.loads(document)` 死代码分支。

### 并发与状态隔离
- 全局单例 Agent 的共享可变状态全部拆解：请求级 MetricsCollector（不再互相 reset）、LLM token 用量随调用返回（`LLMReply`，不再串号）、Tracer 带 session_id 且容量受限（防内存泄漏）、限流按 session_id 隔离。

### 安全与合规
- 输出脱敏前移：最终回答先过滤再入记忆/历史，敏感信息无法经上下文回流。
- 审计日志记录 blocked 事件（此前被拦截输入零留痕）；身份证掩码补齐 X 结尾；计算器改 AST 白名单求值并限制幂运算规模（防 `9**99999999` 卡死事件循环）。
- 长期记忆按 user_id 隔离检索 + 可选距离阈值过滤不相关历史。

### 其他修复
- LLM 客户端：`temperature=0` 不再被 `or 0.7` 吞掉；`MAX_TOKENS` 配置真正下发到 payload。
- 循环引擎：ACTION JSON 解析失败时纠正重试而非把原文当答案；工具失败重试时把失败 thought 一并写入上下文；后台写入任务持引用防 GC。
- 会话历史：临时文件 + `os.replace` 原子写；损坏文件改名备份而非静默覆盖；全链路异步 IO。
- Web 层：全面异步化、新增 `/api/metrics` 与增强 `/health`、清理无用的 `_sessions` 字典、CORS 可配置。

### 前端重写
全新「Agent 控制台」界面（零依赖单文件）：深色主题 + 渐变强调色、思考过程时间线（Thought/Action/Observation 分步展开）、会话侧栏（新建/重命名/删除/localStorage 持久化）、运行指标面板（token/耗时/工具分布柱状图）、工具清单面板、Markdown-lite 渲染、停止生成、响应式布局与 reduced-motion 支持。


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
