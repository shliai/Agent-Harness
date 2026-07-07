from __future__ import annotations

import json
import logging
import time
from collections.abc import AsyncGenerator, Generator

import httpx

from harness.domain.exceptions import LLMError
from harness.domain.models import AgentMessage
from harness.llm.base import AbstractLLMClient

logger = logging.getLogger("harness.llm.openai_compatible")


class OpenAICompatibleClient(AbstractLLMClient):
    def __init__(self, base_url: str, api_key: str, model: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self._chat_url = f"{self.base_url}/chat/completions"
        self.last_token_usage: int = 0
        # 复用连接池：减少 TCP/TLS 握手开销
        self._sync_client = httpx.Client(timeout=60)
        self._async_client = httpx.AsyncClient(timeout=60)
        logger.info("OpenAICompatibleClient 初始化: model=%s base=%s", self.model, self.base_url)

    def chat(self, messages: list[AgentMessage], temperature: float | None = None) -> str:
        t0 = time.perf_counter()
        try:
            payload = {
                "model": self.model,
                "messages": [m.to_llm_format() for m in messages],
                "temperature": temperature or 0.7,
                "stream": False,
            }
            headers = self._headers()
            resp = self._sync_client.post(self._chat_url, headers=headers, json=payload)
            resp.raise_for_status()
            data = resp.json()

            choices = data.get("choices")
            if not choices:
                raise LLMError(f"API 返回空结果: {data}")

            usage = data.get("usage", {})
            self.last_token_usage = usage.get("total_tokens", 0)

            elapsed = (time.perf_counter() - t0) * 1000
            answer = choices[0]["message"]["content"].strip()
            logger.info("LLM 响应: %.0fms | token=%d | model=%s", elapsed, self.last_token_usage, self.model)
            logger.debug("LLM 回答: %s...", answer[:60])
            return answer

        except httpx.HTTPStatusError as e:
            raise LLMError(f"HTTP {e.response.status_code}: {e.response.text[:200]}") from e
        except httpx.TimeoutException as e:
            raise LLMError(f"请求超时: {e}") from e
        except json.JSONDecodeError as e:
            raise LLMError(f"响应解析失败: {e}") from e
        except Exception as e:
            raise LLMError(f"LLM 调用异常: {e}") from e

    async def chat_async(self, messages: list[AgentMessage], temperature: float | None = None) -> str:
        """异步调用 LLM，避免阻塞事件循环"""
        t0 = time.perf_counter()
        try:
            payload = {
                "model": self.model,
                "messages": [m.to_llm_format() for m in messages],
                "temperature": temperature or 0.7,
                "stream": False,
            }
            headers = self._headers()
            resp = await self._async_client.post(self._chat_url, headers=headers, json=payload)
            resp.raise_for_status()
            data = resp.json()

            choices = data.get("choices")
            if not choices:
                raise LLMError(f"API 返回空结果: {data}")

            usage = data.get("usage", {})
            self.last_token_usage = usage.get("total_tokens", 0)

            elapsed = (time.perf_counter() - t0) * 1000
            answer = choices[0]["message"]["content"].strip()
            logger.info("LLM 异步响应: %.0fms | token=%d | model=%s", elapsed, self.last_token_usage, self.model)
            logger.debug("LLM 回答: %s...", answer[:60])
            return answer

        except httpx.HTTPStatusError as e:
            raise LLMError(f"HTTP {e.response.status_code}: {e.response.text[:200]}") from e
        except httpx.TimeoutException as e:
            raise LLMError(f"请求超时: {e}") from e
        except json.JSONDecodeError as e:
            raise LLMError(f"响应解析失败: {e}") from e
        except Exception as e:
            raise LLMError(f"LLM 调用异常: {e}") from e

    def stream_chat(self, messages: list[AgentMessage], temperature: float | None = None) -> Generator[str, None, None]:
        try:
            payload = {
                "model": self.model,
                "messages": [m.to_llm_format() for m in messages],
                "temperature": temperature or 0.7,
                "stream": True,
            }
            headers = self._headers()
            with httpx.stream("POST", self._chat_url, headers=headers, json=payload, timeout=120) as resp:
                resp.raise_for_status()
                for line in resp.iter_lines():
                    if not line.startswith("data: "):
                        continue
                    chunk = line[6:]
                    if chunk == "[DONE]":
                        break
                    try:
                        data = json.loads(chunk)
                        delta = data.get("choices", [{}])[0].get("delta", {}).get("content", "")
                        if delta:
                            yield delta
                    except json.JSONDecodeError:
                        continue

        except Exception as e:
            raise LLMError(f"流式调用失败: {e}") from e

    async def stream_chat_async(self, messages: list[AgentMessage], temperature: float | None = None) -> AsyncGenerator[str, None]:
        """异步流式调用 LLM"""
        try:
            payload = {
                "model": self.model,
                "messages": [m.to_llm_format() for m in messages],
                "temperature": temperature or 0.7,
                "stream": True,
            }
            headers = self._headers()
            async with self._async_client.stream("POST", self._chat_url, headers=headers, json=payload) as resp:
                resp.raise_for_status()
                async for line in resp.aiter_lines():
                    if not line.startswith("data: "):
                        continue
                    chunk = line[6:]
                    if chunk == "[DONE]":
                        break
                    try:
                        data = json.loads(chunk)
                        delta = data.get("choices", [{}])[0].get("delta", {}).get("content", "")
                        if delta:
                            yield delta
                    except json.JSONDecodeError:
                        continue

        except Exception as e:
            raise LLMError(f"异步流式调用失败: {e}") from e

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
