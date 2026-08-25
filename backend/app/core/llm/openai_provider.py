"""OpenAI 兼容接口实现。国内多数模型服务都提供这套协议。"""

import json
from typing import AsyncIterator

import httpx

from app.core.llm.client import ChatChunk, ChatRequest, LLMError, LLMFatalError, ToolCall

# 分类本身就是答案的一部分：401 重试一百次也还是 401，
# 而 429 退避之后大概率能过。
RETRYABLE_STATUS = {408, 409, 425, 429, 500, 502, 503, 504}
FATAL_STATUS = {400, 401, 403, 404, 422}


class OpenAIProvider:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float = 60.0,
    ) -> None:
        self.model = model
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._client = httpx.AsyncClient(transport=transport, timeout=timeout)

    def _build_payload(self, req: ChatRequest) -> dict:
        payload = {
            "model": req.model or self.model,
            "messages": req.messages,
            "max_tokens": req.max_tokens,
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        if req.tools:
            payload["tools"] = req.tools
        return payload

    async def _raise_for_status(self, response: httpx.Response) -> None:
        if response.status_code < 400:
            return
        await response.aread()
        detail = response.text[:200]
        if response.status_code in FATAL_STATUS:
            raise LLMFatalError(f"模型服务返回 {response.status_code}（不可重试）：{detail}")
        raise LLMError(f"模型服务返回 {response.status_code}：{detail}")

    async def stream(self, req: ChatRequest) -> AsyncIterator[ChatChunk]:
        # index -> {id, name, arguments(str)}
        # 工具参数是逐片流式吐出来的，必须按 index 拼完整再解析
        buffers: dict[int, dict] = {}
        usage: dict | None = None
        finish_reason: str | None = None

        try:
            async with self._client.stream(
                "POST", f"{self._base_url}/chat/completions",
                headers={"Authorization": f"Bearer {self._api_key}"},
                json=self._build_payload(req),
            ) as response:
                await self._raise_for_status(response)

                async for line in response.aiter_lines():
                    if not line.startswith("data: "):
                        continue
                    data = line.removeprefix("data: ").strip()
                    if data == "[DONE]":
                        break

                    try:
                        event = json.loads(data)
                    except json.JSONDecodeError as exc:
                        raise LLMError(f"流式响应不是合法 JSON：{data[:120]}") from exc

                    if event.get("usage"):
                        usage = event["usage"]

                    for choice in event.get("choices", []):
                        delta = choice.get("delta") or {}

                        if delta.get("content"):
                            yield ChatChunk(text_delta=delta["content"])

                        for tc in delta.get("tool_calls") or []:
                            idx = tc.get("index", 0)
                            buf = buffers.setdefault(idx, {"id": "", "name": "", "arguments": ""})
                            if tc.get("id"):
                                buf["id"] = tc["id"]
                            fn = tc.get("function") or {}
                            if fn.get("name"):
                                buf["name"] = fn["name"]
                            if fn.get("arguments"):
                                buf["arguments"] += fn["arguments"]

                        if choice.get("finish_reason"):
                            finish_reason = choice["finish_reason"]

        except httpx.TimeoutException as exc:
            raise LLMError(f"模型服务超时：{exc}") from exc
        except httpx.HTTPError as exc:
            raise LLMError(f"模型服务连接失败：{exc}") from exc

        tool_calls: list[ToolCall] | None = None
        if buffers:
            tool_calls = []
            for idx in sorted(buffers):
                buf = buffers[idx]
                try:
                    args = json.loads(buf["arguments"] or "{}")
                except json.JSONDecodeError as exc:
                    raise LLMError(
                        f"工具 {buf['name']} 的参数不是合法 JSON：{buf['arguments'][:120]}",
                    ) from exc
                tool_calls.append(ToolCall(id=buf["id"], name=buf["name"], arguments=args))

        yield ChatChunk(tool_calls=tool_calls, finish_reason=finish_reason, usage=usage)

    async def aclose(self) -> None:
        await self._client.aclose()
