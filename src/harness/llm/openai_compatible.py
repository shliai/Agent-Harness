from __future__ import annotations

import json
import logging
import time
from collections.abc import AsyncGenerator, Generator

import httpx

from harness.config import settings
from harness.domain.exceptions import LLMError
from harness.domain.models import AgentMessage
from harness.llm.base import AbstractLLMClient, LLMReply

logger = logging.getLogger("harness.llm.openai_compatible")


def _resolve_temperature(temperature: float | None) -> float:
    """None 才回退默认值；显式传 0 是合法配置，不能被 or 吞掉"""
    return temperature if temperature is not None else settings.temperature


class OpenAICompatibleClient(AbstractLLMClient):
    def __init__(self, base_url: str, api_key: str, model: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self._chat_url = f"{self.base_url}/chat/completions"
        # 复用连接池：减少 TCP/TLS 握手开销
        limits = httpx.Limits(max_connections=20, max_keepalive_connections=10)
        self._sync_client = httpx.Client(timeout=60, limits=limits)
        self._async_client = httpx.AsyncClient(timeout=60, limits=limits)
        logger.info("OpenAICompatibleClient 初始化: model=%s base=%s", self.model, self.base_url)

    # ── 非流式（异步） ──────────────────────────────────

    async def chat_async(
        self, messages: list[AgentMessage], temperature: float | None = None
    ) -> LLMReply:
        t0 = time.perf_counter()
        try:
            payload = self._payload(messages, temperature)
            resp = await self._post_with_retry(payload)
            resp.raise_for_status()
            reply = self._parse_reply(resp.json(), elapsed_ms=(time.perf_counter() - t0) * 1000)
            return reply
        except httpx.HTTPStatusError as e:
            raise LLMError(f"HTTP {e.response.status_code}: {e.response.text[:200]}") from e
        except httpx.TimeoutException as e:
            raise LLMError(f"请求超时: {e}") from e
        except json.JSONDecodeError as e:
            raise LLMError(f"响应解析失败: {e}") from e
        except LLMError:
            raise
        except Exception as e:
            raise LLMError(f"LLM 调用异常: {e}") from e

    # ── 流式（异步） ────────────────────────────────────

    async def stream_chat_async(
        self, messages: list[AgentMessage], temperature: float | None = None
    ) -> AsyncGenerator[str, None]:
        try:
            payload = self._payload(messages, temperature)
            payload["stream"] = True
            if settings.stream_include_usage:
                # OpenAI v1 扩展：最后一个 chunk 携带 usage；不支持的供应商会忽略该字段
                payload["stream_options"] = {"include_usage": True}
            async with self._async_client.stream(
                "POST", self._chat_url, headers=self._headers(), json=payload
            ) as resp:
                resp.raise_for_status()
                async for delta in self._aiter_sse(resp.aiter_lines()):
                    yield delta
        except Exception as e:
            raise LLMError(f"异步流式调用失败: {e}") from e

    # ── 内部工具方法 ───────────────────────────────────

    def _payload(self, messages: list[AgentMessage], temperature: float | None) -> dict:
        return {
            "model": self.model,
            "messages": [m.to_llm_format() for m in messages],
            "temperature": _resolve_temperature(temperature),
            "max_tokens": settings.max_tokens,
            "stream": False,
        }

    def _parse_reply(self, data: dict, elapsed_ms: float) -> LLMReply:
        choices = data.get("choices")
        if not choices:
            raise LLMError(f"API 返回空结果: {data}")

        usage = data.get("usage", {})
        total_tokens = int(usage.get("total_tokens", 0))
        answer = (choices[0].get("message") or {}).get("content", "").strip()

        logger.info(
            "LLM 响应: %.0fms | token=%d | model=%s", elapsed_ms, total_tokens, self.model
        )
        logger.debug("LLM 回答: %s...", answer[:60])
        return LLMReply(content=answer, total_tokens=total_tokens)

    @staticmethod
    def _iter_sync_sse(lines) -> Generator[str, None, None]:
        for line in lines:
            if not line.startswith("data: "):
                continue
            chunk = line[6:]
            if chunk == "[DONE]":
                break
            delta = _extract_delta(chunk)
            if delta:
                yield delta

    @staticmethod
    async def _aiter_sse(lines) -> AsyncGenerator[str, None]:
        async for line in lines:
            if not line.startswith("data: "):
                continue
            chunk = line[6:]
            if chunk == "[DONE]":
                break
            _sink_usage(chunk)
            delta = _extract_delta(chunk)
            if delta:
                yield delta

    async def _post_with_retry(self, payload: dict):
        """瞬时错误指数退避重试：超时/连接错误/5xx；4xx 不重试"""
        import asyncio as _aio

        attempts = max(int(getattr(settings, "llm_max_retries", 2)), 0) + 1
        delay = float(getattr(settings, "llm_retry_backoff_sec", 1.0))
        last_exc: Exception | None = None
        for attempt in range(attempts):
            is_last = attempt == attempts - 1
            try:
                resp = await self._async_client.post(
                    self._chat_url, headers=self._headers(), json=payload
                )
                if resp.status_code >= 500 and not is_last:
                    raise httpx.HTTPStatusError(
                        f"transient {resp.status_code}", request=resp.request, response=resp
                    )
                return resp
            except (httpx.TimeoutException, httpx.TransportError) as e:
                last_exc = e
            except httpx.HTTPStatusError as e:
                last_exc = e
            if is_last:
                break
            await _aio.sleep(delay * (2 ** attempt))
        assert last_exc is not None
        raise last_exc

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

    def close(self) -> None:
        self._sync_client.close()

    async def aclose(self) -> None:
        await self._async_client.aclose()


def _extract_usage(chunk: str) -> int:
    """从 SSE chunk 提取 usage.total_tokens（include_usage 时出现在最后一块）"""
    try:
        data = json.loads(chunk)
        return int(data.get("usage", {}).get("total_tokens") or 0)
    except (json.JSONDecodeError, ValueError, TypeError):
        return 0


def _sink_usage(chunk: str) -> None:
    """把流式真实用量写入请求级槽位；无槽位时忽略"""
    total = _extract_usage(chunk)
    if not total:
        return
    try:
        from harness.tools.context import llm_usage_sink

        sink = llm_usage_sink.get()
        if sink is not None:
            sink["total"] = total
            logger.debug("流式 usage 捕获: %s", total)
    except Exception:
        pass


def _extract_delta(chunk: str) -> str:
    try:
        data = json.loads(chunk)
        return data.get("choices", [{}])[0].get("delta", {}).get("content", "")
    except (json.JSONDecodeError, IndexError, KeyError, TypeError):
        return ""
