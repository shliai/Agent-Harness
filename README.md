# Agent Harness

把 LLM 从「问答盒子」变成「能做事的人」——一个轻量但完整的 Agent 运行时外壳。

单纯调 API 做 Demo 很简单，但要落地到真实场景，LLM 需要配套的**运行基础设施**：循环控制、工具管理、记忆持久化、安全护栏、可观测。这个项目就是我对这套基础设施的完整实现。

## 快速开始

```bash
# 安装
pip install -e .

# 配置（填入 API Key）
cp .env.example .env

# 初始化向量知识库（自动下载 BGE 嵌入模型，首次约 2 分钟）
python scripts/seed_products.py

# 启动服务
python -m harness.main --port 8000
# 浏览器打开 http://localhost:8000
```

## Harness 工作一览

完整的 Agent Harness 需要四大构成，本项目每个都有落地实现：

| Harness 构成 | 我的实现 | 设计要点 |
|---|---|---|
| **Agent 循环引擎** | `core/loop.py` 自研 ReAct 闭环 | ~150 行核心逻辑，SSE 流式逐帧推送，失败自动重试 |
| **工具接口层** | `tools/` 5 个工具，统一 BaseTool 注册协议 | 新增工具只需继承 + 声明 spec()，自动注册到 LLM |
| **上下文管理器** | `memory/` 三层记忆架构 | 短期(deque) + 长期(ChromaDB) + 持久化(JSON)，会话隔离 |
| **安全控制机制** | `guardrails/` 4 道护栏流水线 | 输入校验 / 限流 / 输出脱敏 / 审计日志，短路评估 |

除此之外，还覆盖了 Harness 所需的 **可观测**（结构化日志 + 指标 + 调用链追踪）和 **LLM 抽象层**（工厂模式，智谱 / OpenAI 双供应商）。

### 代码量

| 分类 | 文件数 | 行数 |
|---|---|---|
| 核心源码 `src/harness/` | 39 `.py` | 2,994 |
| 前端界面 `index.html` | 1 | 1,820 |
| 测试用例 `tests/` | 11 `.py` | 493 |
| 工具脚本 `scripts/` | 2 `.py` | 83 |
| **总计** | **53** | **5,415** |

其中 harness 核心逻辑（循环引擎 + 工具系统 + 记忆 + 护栏 + 可观测）约占 1,800 行。

## 架构总览

```
┌──────────┐    ┌──────────┐    ┌──────────┐    ┌────────────┐
│  Web API  │───→│  ReAct   │───→│  Tools   │───→│  Memory    │
│ (FastAPI) │    │   Loop   │    │  x5      │    │  三层记忆   │
└──────────┘    └──────────┘    └──────────┘    └────────────┘
                      │
                      ↓
                ┌──────────┐    ┌──────────────┐
                │ Guardrails│───→│ Observability│
                │  4道关卡   │    │ 日志/指标/追踪 │
                └──────────┘    └──────────────┘
```

完整链路：Guardrails 拦截 → 记忆装配 → LLM 推理 → 工具执行（可选循环） → 输出过滤 → 持久化 → 流式返回。

## Harness 模块详解

### 循环引擎 — 自研 ReAct

没有用 LangChain / LangGraph，核心循环自己实现，每步都可控：

- **SSE 流式推送**：每轮 Thought → Action → Observation 逐帧推送到前端，不是等全部跑完再返回
- **工具调用解析**：LLM 输出格式不稳定，用 `json.JSONDecoder.raw_decode` 兜底，兼容各种变体
- **失败重试**：工具调用失败后让 LLM 修正参数重试，而不是直接抛异常
- **终止控制**：LLM 直接回答 或 超过 MAX_ITERATIONS 上限即停止，防止死循环

### 工具接口层 — 插拔式注册

```python
class BaseTool(ABC):
    @abstractmethod
    def run(self, **kwargs) -> ToolResult: ...
    @abstractmethod
    def spec(self) -> ToolSpec: ...
```

新增工具只需继承 BaseTool + 声明 spec()，自动注册到 LLM 的 tool calling 协议。`registry.py` 维护工具名到实现的映射，供循环引擎路由调用。目前 5 个：

| 工具 | 说明 | 要点 |
|---|---|---|
| `knowledge_retrieval` | 商品知识检索 | ChromaDB 向量 + BM25 混合，双编码器分数归一化 |
| `order_query` | 订单查询 | 50 条模拟订单 |
| `logistics_query` | 物流轨迹 | 41 个单号，多节点轨迹 |
| `calculator` | 数学计算 | 正则白名单，只允许 +-*/()% 和数字 |
| `subtask_dispatch` | 子任务分发 | 防递归自检，禁止调用自身 |

### 上下文管理器 — 三层记忆

LLM 上下文窗口有限，不能把所有历史都塞进去：

- **短期记忆**：`deque` 滑动窗口，保留最近 N 轮，超出丢弃最旧的（O(1) 头部删除）
- **长期记忆**：ChromaDB + BGE 嵌入，语义检索召回相关历史
- **持久化**：JSON 文件保存全量对话，24h 自动清理过期会话
- **会话隔离**：按 session_id 独立上下文，互不污染

### 安全护栏 — 流水线短路

```
InputValidator → RateLimiter → AuditLogger → OutputFilter
```

流水线顺序执行，任一护栏拦截即短路停止。三入口覆盖全生命周期：

- `check_input()` — 用户输入阶段：空值 / 超长 / 控制字符校验
- `check_tool_output()` — 工具返回阶段：工具结果脱敏
- `check_output()` — 最终输出阶段：身份证 / 手机号 / 银行卡 / API Key 正则掩码

### 可观测

- **结构化日志**：console / json 双格式，生产环境用 json 方便采集
- **指标统计**：Token 消耗、工具调用次数、各步骤耗时
- **调用链追踪**：按 session_id 聚合每轮对话的完整决策路径

## 项目结构

```
src/harness/
├── core/              ReAct 循环引擎
├── llm/               LLM 客户端抽象（工厂模式）
├── tools/             5 个工具 + 注册中心
├── memory/            三层记忆 + 会话管理
├── guardrails/        4 道护栏流水线
├── observability/     日志 / 指标 / 追踪
├── domain/            核心模型 + 分层异常
├── web/               FastAPI + SSE 流式 + 聊天界面
├── config.py          Pydantic Settings 配置中心
└── main.py            启动入口
```

各层单向依赖，核心层不感知 Web 层，通过接口抽象解耦。

## API 端点

| 方法 | 路径 | 说明 |
|---|---|---|
| `GET` | `/health` | 健康检查 |
| `GET` | `/api/tools` | 工具列表 |
| `POST` | `/api/chat` | 聊天（SSE 流式响应） |
| `POST` | `/api/session/clear` | 清空会话 |
| `GET` | `/api/sessions` | 会话列表 |
| `GET` | `/api/sessions/{id}` | 会话详情 |
| `PUT` | `/api/sessions/{id}` | 重命名会话 |
| `DELETE` | `/api/sessions/{id}` | 删除会话 |

## 配置项

| 环境变量 | 默认值 | 说明 |
|---|---|---|
| `LLM_PROVIDER` | `zhipu` | 供应商选择 |
| `ZHIPU_API_KEY` | — | 智谱 API Key |
| `OPENAI_API_KEY` | — | OpenAI API Key |
| `TEMPERATURE` | `0.3` | 生成温度 |
| `MAX_TOKENS` | `2048` | 最大 Token |
| `MAX_ITERATIONS` | `6` | ReAct 最大步数 |
| `SHORT_TERM_WINDOW` | `20` | 短期记忆窗口 |
| `LONG_TERM_ENABLED` | `false` | 长期记忆开关 |
| `RATE_LIMIT_MAX_REQUESTS` | `60` | 每分钟最大请求 |
| `HYBRID_SEARCH_ALPHA` | `0.5` | 混合检索 BM25 权重 |
| `RETRIEVAL_TOP_K` | `5` | 知识检索返回条数 |

## 测试

```bash
pytest tests/ -v
```

37 个用例（29 单元 + 8 集成），覆盖核心循环、工具执行、护栏检查、API 端点。

## 演示场景

```
# 知识检索
> 5000元以下的拍照手机

# 订单查询
> 查询订单 20240601001

# 物流跟踪
> 帮我查一下快递 SF1234567890 到哪了

# 多步骤任务（触发子任务分发）
> 我想买一个预算3000元以内的手机，主要用来拍照
```

## 设计取舍

- **为什么自研不用 LangChain？** — 框架封装太厚，循环被黑盒化，出问题难排查。自研 ~150 行核心，每一行都可控
- **为什么 SSE 而不是 WebSocket？** — AI 回复是单工流，SSE 更轻量，浏览器原生支持 EventSource
- **为什么 JSON 做持久化？** — Demo 阶段减少外部依赖，接口已抽象，换 PostgreSQL / Redis 只需实现接口
- **为什么向量 + BM25 双检索？** — 向量擅长语义相似，BM25 擅长关键词精确匹配，互补效果好