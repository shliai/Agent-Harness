# Agent Harness API 文档

> 版本：v0.7.7 · 更新日期：2026-08-27

## 基础信息

- **基础 URL**: `http://localhost:8000`
- **数据格式**: JSON
- **认证**: 聊天/会话接口无需认证；商品管理接口需 `X-Admin-Token` 请求头（配置项 `ADMIN_TOKEN`，限流 30 次/分钟）
- **交互协议**: 聊天默认 SSE 流式

## 端点总览

| 方法 | 路径 | 说明 | 鉴权 |
|---|---|---|---|
| GET | `/` | Web 控制台页面 | - |
| GET | `/health` | 健康检查（含组件状态） | - |
| GET | `/api/tools` | 工具清单（9 个） | - |
| GET | `/api/metrics` | 进程级聚合指标 + 最近追踪 | - |
| POST | `/api/chat` | 对话（SSE 流式 / JSON） | - |
| POST | `/api/session/clear` | 清空会话 | - |
| POST | `/api/sessions/batch-delete` | 批量删除会话（body: ids 数组；越权跳过） | - |
| GET | `/api/sessions` | 会话列表 | - |
| GET | `/api/sessions/{id}` | 会话详情（含推理轨迹） | - |
| PUT | `/api/sessions/{id}` | 重命名会话 | - |
| DELETE | `/api/sessions/{id}` | 删除会话 | - |
| POST | `/api/admin/products` | 新增商品（联动向量库） | Admin |
| PUT | `/api/admin/products/{pid}` | 更新商品（重嵌入） | Admin |
| DELETE | `/api/admin/products/{pid}` | 删除商品（DB+向量同步删） | Admin |
| GET | `/api/admin/products?status=` | 商品列表 | Admin |
| POST | `/api/admin/products/reindex` | 向量库对账式全量重建 | Admin |
| GET | `/api/admin/aftersales?status=` | 售后单列表（商家视图） | Admin |
| POST | `/api/admin/aftersales/{as_id}/approve` | 审批通过（body: note） | Admin |
| POST | `/api/admin/aftersales/{as_id}/reject` | 驳回（body: note 必填原因） | Admin |
| POST | `/api/admin/aftersales/{as_id}/complete` | 完成打款（已通过→已完成） | Admin |

> 所有 `{id}` 路径参数均做白名单校验 `^[A-Za-z0-9_-]{1,64}$`，非法值返回 400。

---

## 1. 健康检查

### GET `/health`

```json
{
  "status": "ok",
  "version": "0.7.7",
  "components": {
    "knowledge_base_documents": 400,
    "learning_enabled": false,
    "learning_records": 0,
    "tools": 9
  }
}
```

## 2. 对话 POST `/api/chat`

**请求体**：

| 参数 | 类型 | 必需 | 说明 |
|---|---|---|---|
| message | string | 是 | 用户消息（1-4096 字符） |
| session_id | string | 否 | 会话 ID；缺省自动生成并经 meta 事件返回 |
| user_id | string | 否 | 用户身份：决定订单归属校验、我的订单、售后归属；缺省为空，后端以 `demo_user` 作为默认用户身份处理（学习机制为单用户，不按 user_id 隔离） |
| stream | boolean | 否 | 默认 true |

**同会话并发约束**：同一 session_id 同时仅允许一个进行中请求，
流式下返回 `error` 事件、非流式返回 HTTP 429。

### 流式响应事件（SSE，按发生顺序）

```
data: {"type":"meta","session_id":"abc123"}

# token 级增量（每轮 LLM 输出实时推送）
data: {"type":"delta","content":"您好"}

# 该轮实为工具规划 → 回滚已推送的临时文本
data: {"type":"delta_reset"}   # 可选携带 reason: {"type":"delta_reset","reason":"..."}

# 每完成一步推送（Thought / Action / Observation）
data: {"type":"step","step_index":0,"thought":"需要查订单",
       "tool_call":{"tool_name":"order_query","arguments":{"order_id":"2026082200131"}},
       "tool_result":{"success":true,"output":"订单号：...","duration_ms":12.5}}

# 最终回答经脱敏后若与流式原文不一致 → 整体覆盖
data: {"type":"answer_replace","content":"您的手机号***已登记..."}

data: {"type":"result","answer":"...","total_duration_ms":3200.5,
       "total_steps":2,"total_tokens":4410,"success":true}

data: [DONE]
```

错误以 `{"type":"error","message":"..."}` 事件推送（如限流/护栏拦截/上游超时），随后仍发送 `[DONE]`。

### 非流式响应（stream=false）

```json
{
  "answer": "...",
  "session_id": "abc123",
  "steps": [{"step_index": 0, "thought": "...", "tool_call": {...}, "tool_result": {...}}],
  "total_duration_ms": 3200.5,
  "total_tokens": 4410,
  "success": true,
  "error": null
}
```


## 3. 会话管理

### GET `/api/sessions`

按更新时间倒序返回：

```json
{"sessions": [{"id": "abc123", "title": "查询订单", "updated_at": "...", "message_count": 6}]}
```

### GET `/api/sessions/{id}`

完整会话状态（前端据此回放对话与推理轨迹）：

```json
{
  "session_id": "abc123",
  "title": "查询订单",
  "user_id": "demo_user",
  "working_memory": {"budget_amount": 3000.0, "order_ids": ["..."], "rolling_summary": "【诉求】...【进展】...【未决】...", "awaiting_slot": null},
  "traces": [{"ts": "...", "user": "查订单", "steps": [{"thought": "...", "tool_call": {...}}]}],
  "messages": [{"role": "user", "content": "...", "timestamp": "..."}],
  "updated_at": "..."
}
```

| 字段 | 说明 |
|---|---|
| user_id | 会话归属者；携带 X-User-Id 访问他人会话将返回 403 |
| working_memory | 结构化槽位：预算(金额/品类/轮次)、订单号、物流号、滚动摘要(rolling_summary,≤300字折叠摘要)、澄清等待项 |
| traces | 推理轨迹（Thought/Action/Observation） |

### PUT `/api/sessions/{id}`

```json
{"title": "新名称"}
```

### POST `/api/session/clear` · DELETE `/api/sessions/{id}`

清空与删除均同步删除消息与状态，返回 `{"status": "cleared"/"deleted"}`。

## 4. 商品管理（需 `X-Admin-Token` 头）

超限返回 HTTP 429；鉴权失败返回 403。

### POST `/api/admin/products`

```json
{
  "name": "小米17 Pro",
  "category": "手机",
  "brand": "小米",
  "price": 5499,
  "description": "骁龙8 Elite2 旗舰",
  "specs": {"屏幕": "6.73英寸", "存储": "256GB"},
  "tags": ["旗舰", "拍照"],
  "status": "在售"
}
```

响应：`{"status": "created", "id": "product_xxxxxxxxxx"}`

行为：写 SQLite → 文档渲染 → BGE 编码 → 向量库 upsert（全程联动，无需手动重建索引）。

### PUT `/api/admin/products/{pid}`

请求体同上；upsert 幂等覆盖，语义字段变化自动重嵌入。404=商品不存在。

### DELETE `/api/admin/products/{pid}`

**DB 与向量索引同步删除**（下架残留双保险之一）。404=不存在。

### GET `/api/admin/products?status=在售`

```json
{"products": [{...}], "total": 62}
```

### POST `/api/admin/products/reindex`

以 SQLite 为事实源对账式重建：全量 upsert + 清理向量库脏 id。

```json
{"status": "reindexed", "upserted": 62, "pruned": 0, "final_count": 62}
```

## 5. 指标与追踪

### GET `/api/metrics`

```json
{
  "metrics": {
    "total_llm_calls": 42, "total_tokens": 51000,
    "tool_call_counts": {"knowledge_retrieval": 12, "order_query": 8},
    "total_duration_ms": 98000, "uptime_seconds": 3600
  },
  "recent_traces": [ ... 最近 50 条步骤记录（带 session_id）... ]
}
```

## 错误码约定

| 码 | 场景 |
|---|---|
| 400 | session_id 非法 |
| 403 | 管理 Token 错误；或向他人会话写入 / 读取他人会话 |
| 404 | 会话/商品不存在 |
| 429 | 同会话请求并发冲突 / 管理接口限流 |
| 500 | 未预期异常（日志含堆栈） |

业务级失败（订单不属于当前账户 / 待发货不可售后 / 政策库未命中引导转人工 等）
不以 HTTP 错误表达，而是作为工具输出文本交由 LLM 组织回复。
