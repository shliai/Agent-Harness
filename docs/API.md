# Agent Harness API 文档

## 概述

本文档提供了 Agent Harness 演示系统的 API 接口说明，帮助开发者了解和使用系统。

## 基础信息

- **基础URL**: `http://localhost:8000`
- **数据格式**: JSON
- **认证方式**: 演示系统无需认证
- **系统类型**: 电商客服场景 ReAct 循环演示系统

## 端点列表

### 1. 健康检查

#### GET `/`

**描述**: 返回 Web 聊天界面（HTML 页面从 `src/harness/web/static/index.html` 加载）

**请求**:
```bash
GET /
```

**响应**: 200 — HTML 页面

#### GET `/health`

**描述**: 健康检查端点

**请求**:
```bash
GET /health
```

**响应**:
```json
{
  "status": "ok",
  "version": "0.1.0"
}
```

### 2. 聊天接口

#### POST `/api/chat`

**描述**: 发送消息给 Agent，支持 SSE 流式响应

**请求体**:
```json
{
  "message": "你好，我想查询商品信息",
  "session_id": "optional_session_id",
  "stream": true
}
```

**参数**:
| 参数 | 类型 | 必需 | 默认值 | 描述 |
|---|---|---|---|---|
| `message` | string | 是 | - | 用户消息（1-4096 字符） |
| `session_id` | string | 否 | 自动生成 | 会话ID |
| `stream` | boolean | 否 | true | 是否使用流式响应 |

**流式响应（SSE）**:
```
data: {"type":"meta","session_id":"abc123"}

data: {"type":"step","step_index":0,"thought":"用户需要查询商品信息","tool_call":null,"tool_result":null}

data: {"type":"step","step_index":1,"thought":"需要使用知识检索工具","tool_call":{"tool_name":"knowledge_retrieval","arguments":{"query":"拍照手机"}},"tool_result":{"success":true,"output":"查询结果","duration_ms":500}}

data: {"type":"result","answer":"根据您的查询，我找到了以下商品...","total_duration_ms":1500,"total_steps":2,"success":true}

data: [DONE]
```

**SSE 事件类型**:
| 事件 | 触发时机 | 关键字段 |
|------|---------|---------|
| `meta` | 连接建立后 | session_id |
| `step` | 每个 ReAct 步骤 | step_index, thought, tool_call, tool_result |
| `result` | 最终结果 | answer, total_duration_ms, total_steps, success |
| `error` | 发生异常 | message |
| `[DONE]` | 流结束 | - |

**普通响应**（stream=false）:
```json
{
  "answer": "根据您的查询，我找到了以下商品...",
  "session_id": "abc123",
  "steps": [
    {
      "step_index": 0,
      "thought": "用户需要查询商品信息",
      "tool_call": null,
      "tool_result": null
    },
    {
      "step_index": 1,
      "thought": "需要使用知识检索工具",
      "tool_call": {
        "tool_name": "knowledge_retrieval",
        "arguments": {"query": "拍照手机"}
      },
      "tool_result": {
        "success": true,
        "output": "查询结果",
        "duration_ms": 500
      }
    }
  ],
  "total_duration_ms": 1500,
  "success": true,
  "error": null
}
```

### 3. 工具管理

#### GET `/api/tools`

**描述**: 获取所有已注册工具的详细信息

**请求**:
```bash
GET /api/tools
```

**响应**:
```json
{
  "tools": [
    {
      "name": "knowledge_retrieval",
      "description": "检索电商商品知识库，用于回答商品信息、价格查询、参数对比等",
      "parameters": {
        "type": "object",
        "properties": {
          "query": {"type": "string", "description": "用户问题"},
          "category": {"type": "string", "description": "商品类别"},
          "price_min": {"type": "number", "description": "最低价格"},
          "price_max": {"type": "number", "description": "最高价格"}
        },
        "required": ["query"]
      }
    },
    {
      "name": "order_query",
      "description": "根据订单号查询订单详情",
      "parameters": {
        "type": "object",
        "properties": {
          "order_id": {"type": "string", "description": "订单号，如 20240601001"}
        },
        "required": ["order_id"]
      }
    },
    {
      "name": "logistics_query",
      "description": "查询物流轨迹，返回详细运输节点和状态",
      "parameters": {
        "type": "object",
        "properties": {
          "logistics_no": {"type": "string", "description": "物流单号，如 SF1234567890"}
        },
        "required": ["logistics_no"]
      }
    },
    {
      "name": "calculator",
      "description": "执行数学计算，支持加减乘除、幂运算等",
      "parameters": {
        "type": "object",
        "properties": {
          "expression": {"type": "string", "description": "数学表达式，如 2 + 3 * 4"}
        },
        "required": ["expression"]
      }
    },
    {
      "name": "subtask_dispatch",
      "description": "将复杂任务拆分为多个子任务，逐一分发执行并汇总",
      "parameters": {
        "type": "object",
        "properties": {
          "tasks": {
            "type": "array",
            "description": "子任务列表",
            "items": {
              "type": "object",
              "properties": {
                "id": {"type": "string"},
                "description": {"type": "string"},
                "tools": {"type": "array", "items": {"type": "string"}}
              }
            }
          }
        },
        "required": ["tasks"]
      }
    }
  ]
}
```

### 4. 会话管理

#### GET `/api/sessions`

**描述**: 获取所有会话列表

**请求**:
```bash
GET /api/sessions
```

**响应**:
```json
{
  "sessions": [
    {
      "id": "abc123",
      "title": "查询订单信息",
      "updated_at": "2026-07-01T09:30:00",
      "message_count": 5
    }
  ]
}
```

#### GET `/api/sessions/{id}`

**描述**: 获取指定会话的完整历史记录

**请求**:
```bash
GET /api/sessions/abc123
```

**响应**: 返回存储在 `data/conversations/{id}.json` 的完整内容
```json
{
  "session_id": "abc123",
  "updated_at": "2026-07-01T09:30:00",
  "messages": [
    {"role": "user", "content": "你好"},
    {"role": "assistant", "content": "你好！有什么可以帮助您的吗？"}
  ]
}
```

#### PUT `/api/sessions/{id}`

**描述**: 重命名会话

**请求**:
```bash
curl -X PUT http://localhost:8000/api/sessions/abc123 \
  -H "Content-Type: application/json" \
  -d '{"title": "新的会话标题"}'
```

**响应**:
```json
{
  "status": "renamed",
  "title": "新的会话标题"
}
```

#### DELETE `/api/sessions/{id}`

**描述**: 删除指定会话

**请求**:
```bash
DELETE /api/sessions/abc123
```

**响应**:
```json
{
  "status": "deleted"
}
```

#### POST `/api/session/clear`

**描述**: 清空指定会话的记忆

**请求**:
```json
{
  "session_id": "abc123"
}
```

**响应**:
```json
{
  "status": "cleared",
  "session_id": "abc123"
}
```

## 错误处理

### 错误响应格式

错误通过以下方式返回：
- **HTTP 404**: 会话不存在时返回
- **SSE error 事件**: 流式响应中发生异常时推送
- **非流式响应**: error 字段不为 null

### SSE 错误示例

```
data: {"type":"error","message":"输入内容不能为空"}

data: [DONE]
```

### 常见错误

| 场景 | HTTP 状态码 | 说明 |
|-----|------------|------|
| 输入为空 | 422 | message 字段为空 |
| 输入超长 | 422 | message 超过 4096 字符 |
| 会话不存在 | 404 | 指定的 session_id 无对应文件 |
| 速率超限 | 429 | 超过 60 次/分钟的限制 |
| 服务器错误 | 500 | 内部异常 |

## 安全考虑

### 输入验证

- 所有输入经过 InputValidator 检查
- 拦截空内容、超长输入（>4096 字符）、控制字符

### 输出过滤

- OutputFilter 自动脱敏敏感信息
- 正则匹配并替换为 `***`：身份证号（18位）、手机号（11位）、银行卡号（16-19位）、API Key（sk-开头）

### 速率限制

- RateLimiter 滑动窗口算法
- 默认限制：60 次/60 秒
- 超限抛出 RateLimitError

### 审计日志

- AuditLogger 记录所有 guardrail 检查
- 写入 `data/audit_logs/audit_{date}.jsonl`

## 示例代码

### Python 示例

```python
import requests
import json

# 发送消息（流式）
response = requests.post(
    "http://localhost:8000/api/chat",
    json={"message": "你好", "session_id": "test_session", "stream": True},
    stream=True
)

for line in response.iter_lines():
    if not line:
        continue
    line = line.decode("utf-8")
    if line.startswith("data: "):
        data_str = line[6:]
        if data_str == "[DONE]":
            break
        data = json.loads(data_str)
        if data["type"] == "result":
            print(f"答案: {data['answer']}")
        elif data["type"] == "step":
            print(f"步骤 {data['step_index']}: {data['thought']}")

# 获取工具列表
response = requests.get("http://localhost:8000/api/tools")
tools = response.json()["tools"]
print(f"可用工具: {len(tools)} 个")
```

### JavaScript 示例

```javascript
async function sendMessage(message, sessionId = null) {
    const response = await fetch("/api/chat", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({message, session_id: sessionId, stream: true})
    });

    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";

    while (true) {
        const {done, value} = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, {stream: true});
        const lines = buffer.split("\n");
        buffer = lines.pop();

        for (const line of lines) {
            if (line.startsWith("data: ")) {
                const payload = line.slice(6);
                if (payload === "[DONE]") return;
                const data = JSON.parse(payload);
                if (data.type === "result") {
                    console.log("答案:", data.answer);
                } else if (data.type === "step") {
                    console.log(`步骤 ${data.step_index}:`, data.thought);
                }
            }
        }
    }
}
```

---

**最后更新**: 2026-07-01 (v0.1.0)