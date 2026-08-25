"""LLM Provider：流式解析、工具调用拼装、错误分类。

全程用 MockTransport 打假响应，不需要真实 API key，也不会打真实网络。
"""

import json

import httpx
import pytest

from app.core.llm.client import ChatRequest, LLMError, LLMFatalError
from app.core.llm.openai_provider import OpenAIProvider


def _sse(payloads: list[dict], done: bool = True) -> bytes:
    body = "".join(f"data: {json.dumps(p)}\n\n" for p in payloads)
    if done:
        body += "data: [DONE]\n\n"
    return body.encode()


def _provider(handler) -> OpenAIProvider:
    return OpenAIProvider("http://fake/v1", "k", "m", transport=httpx.MockTransport(handler))


def _responds(payloads: list[dict], done: bool = True):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=_sse(payloads, done),
                              headers={"content-type": "text/event-stream"})
    return handler


async def _collect(provider: OpenAIProvider) -> list:
    return [c async for c in provider.stream(ChatRequest(messages=[], tools=[], model="m"))]


class TestTextStreaming:
    @pytest.mark.asyncio
    async def test_yields_text_deltas(self):
        chunks = await _collect(_provider(_responds([
            {"choices": [{"delta": {"content": "你"}, "finish_reason": None}]},
            {"choices": [{"delta": {"content": "好"}, "finish_reason": "stop"}]},
        ])))
        assert "".join(c.text_delta for c in chunks if c.text_delta) == "你好"
        assert chunks[-1].finish_reason == "stop"

    @pytest.mark.asyncio
    async def test_captures_usage(self):
        chunks = await _collect(_provider(_responds([
            {"choices": [{"delta": {"content": "hi"}, "finish_reason": "stop"}]},
            {"choices": [], "usage": {"prompt_tokens": 100, "completion_tokens": 5}},
        ])))
        assert chunks[-1].usage == {"prompt_tokens": 100, "completion_tokens": 5}

    @pytest.mark.asyncio
    async def test_empty_stream_does_not_crash(self):
        chunks = await _collect(_provider(_responds([])))
        assert chunks[-1].tool_calls is None


class TestToolCallAssembly:
    @pytest.mark.asyncio
    async def test_assembles_fragmented_arguments(self):
        """工具参数是逐片流式吐出来的，必须按 index 拼完整再解析。"""
        chunks = await _collect(_provider(_responds([
            {"choices": [{"delta": {"tool_calls": [
                {"index": 0, "id": "call_1",
                 "function": {"name": "calc_macros", "arguments": ""}}]}}]},
            {"choices": [{"delta": {"tool_calls": [
                {"index": 0, "function": {"arguments": '{"tdee":'}}]}}]},
            {"choices": [{"delta": {"tool_calls": [
                {"index": 0, "function": {"arguments": '2600}'}}]}}]},
            {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
        ])))
        final = chunks[-1]
        assert final.finish_reason == "tool_calls"
        assert final.tool_calls[0].name == "calc_macros"
        assert final.tool_calls[0].arguments == {"tdee": 2600}
        assert final.tool_calls[0].id == "call_1"

    @pytest.mark.asyncio
    async def test_parallel_tool_calls_kept_separate(self):
        """并行工具调用按 index 区分，不能串到一起。"""
        chunks = await _collect(_provider(_responds([
            {"choices": [{"delta": {"tool_calls": [
                {"index": 0, "id": "a", "function": {"name": "t1", "arguments": '{"x":1}'}},
                {"index": 1, "id": "b", "function": {"name": "t2", "arguments": '{"y":2}'}}]}}]},
            {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
        ])))
        calls = chunks[-1].tool_calls
        assert [c.name for c in calls] == ["t1", "t2"]
        assert calls[0].arguments == {"x": 1}
        assert calls[1].arguments == {"y": 2}

    @pytest.mark.asyncio
    async def test_empty_arguments_becomes_empty_dict(self):
        """无参工具的 arguments 可能是空串，不能让 json.loads 炸掉。"""
        chunks = await _collect(_provider(_responds([
            {"choices": [{"delta": {"tool_calls": [
                {"index": 0, "id": "a", "function": {"name": "noarg", "arguments": ""}}]}}]},
            {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
        ])))
        assert chunks[-1].tool_calls[0].arguments == {}

    @pytest.mark.asyncio
    async def test_malformed_arguments_raise_retryable_error(self):
        """模型吐了非法 JSON —— 归类为可重试，交给降级层。"""
        with pytest.raises(LLMError):
            await _collect(_provider(_responds([
                {"choices": [{"delta": {"tool_calls": [
                    {"index": 0, "id": "c", "function": {"name": "f", "arguments": "{broken"}}]}}]},
                {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
            ])))


class TestErrorClassification:
    """分类本身就是答案的一部分：401 重试一百次也还是 401，
    而 429 退避之后大概率能过。"""

    @pytest.mark.parametrize("status", [408, 429, 500, 502, 503, 504])
    @pytest.mark.asyncio
    async def test_retryable_statuses(self, status):
        with pytest.raises(LLMError):
            await _collect(_provider(
                lambda r: httpx.Response(status, json={"error": "x"}),
            ))

    @pytest.mark.parametrize("status", [400, 401, 403, 404, 422])
    @pytest.mark.asyncio
    async def test_fatal_statuses(self, status):
        with pytest.raises(LLMFatalError):
            await _collect(_provider(
                lambda r: httpx.Response(status, json={"error": "bad"}),
            ))

    @pytest.mark.asyncio
    async def test_fatal_is_not_a_subclass_of_retryable(self):
        """降级层靠 except LLMError 决定重试，若 Fatal 是其子类就会被误重试。"""
        assert not issubclass(LLMFatalError, LLMError)

    @pytest.mark.asyncio
    async def test_timeout_is_retryable(self):
        def handler(request):
            raise httpx.ReadTimeout("timed out")
        with pytest.raises(LLMError):
            await _collect(_provider(handler))

    @pytest.mark.asyncio
    async def test_connection_error_is_retryable(self):
        def handler(request):
            raise httpx.ConnectError("refused")
        with pytest.raises(LLMError):
            await _collect(_provider(handler))

    @pytest.mark.asyncio
    async def test_malformed_sse_line_is_retryable(self):
        def handler(request):
            return httpx.Response(200, content=b"data: {not json}\n\n",
                                  headers={"content-type": "text/event-stream"})
        with pytest.raises(LLMError):
            await _collect(_provider(handler))


class TestRequestShape:
    @pytest.mark.asyncio
    async def test_sends_tools_only_when_present(self):
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen.update(json.loads(request.content))
            return httpx.Response(200, content=_sse([
                {"choices": [{"delta": {"content": "ok"}, "finish_reason": "stop"}]},
            ]), headers={"content-type": "text/event-stream"})

        p = OpenAIProvider("http://fake/v1", "k", "m", transport=httpx.MockTransport(handler))
        _ = [c async for c in p.stream(ChatRequest(messages=[{"role": "user", "content": "hi"}]))]
        assert "tools" not in seen
        assert seen["stream"] is True

    @pytest.mark.asyncio
    async def test_sends_auth_header(self):
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["auth"] = request.headers.get("authorization")
            return httpx.Response(200, content=_sse([
                {"choices": [{"delta": {}, "finish_reason": "stop"}]},
            ]), headers={"content-type": "text/event-stream"})

        p = OpenAIProvider("http://fake/v1", "secret-key", "m",
                           transport=httpx.MockTransport(handler))
        _ = [c async for c in p.stream(ChatRequest(messages=[]))]
        assert seen["auth"] == "Bearer secret-key"

    @pytest.mark.asyncio
    async def test_base_url_trailing_slash_tolerated(self):
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["url"] = str(request.url)
            return httpx.Response(200, content=_sse([
                {"choices": [{"delta": {}, "finish_reason": "stop"}]},
            ]), headers={"content-type": "text/event-stream"})

        p = OpenAIProvider("http://fake/v1/", "k", "m", transport=httpx.MockTransport(handler))
        _ = [c async for c in p.stream(ChatRequest(messages=[]))]
        assert seen["url"] == "http://fake/v1/chat/completions"


class TestFactory:
    def test_raises_clear_error_when_llm_not_configured(self, monkeypatch):
        """LLM 未配置时报明确错误，而不是把空模型名发出去等 API 回一个看不懂的错。"""
        from app.core.config import get_settings
        from app.core.llm.factory import build_provider

        get_settings.cache_clear()
        monkeypatch.setenv("LLM_API_KEY", "")
        monkeypatch.setenv("LLM_MODEL", "")
        try:
            with pytest.raises(RuntimeError) as exc:
                build_provider()
            assert "LLM_API_KEY" in str(exc.value)
        finally:
            get_settings.cache_clear()
