"""降级阶梯 L1-L3。"""

import pytest

from app.core.llm.client import ChatChunk, ChatRequest, LLMError, LLMFatalError
from app.core.llm.with_fallback import CORE_TOOL_NAMES, FallbackProvider


class FakeProvider:
    """按脚本决定第 n 次调用是成功还是抛错。"""

    def __init__(self, model: str, script: list) -> None:
        self.model = model
        self.script = script
        self.calls = 0
        self.last_tools: list[dict] | None = None

    async def stream(self, req: ChatRequest):
        self.last_tools = req.tools
        outcome = self.script[min(self.calls, len(self.script) - 1)]
        self.calls += 1
        if isinstance(outcome, Exception):
            raise outcome
        yield ChatChunk(text_delta=outcome)
        yield ChatChunk(finish_reason="stop")


async def _collect(fp: FallbackProvider, req: ChatRequest | None = None) -> tuple[str, int]:
    text, level = "", 0
    async for chunk, lv in fp.stream_with_fallback(req or ChatRequest(messages=[])):
        level = max(level, lv)
        if chunk.text_delta:
            text += chunk.text_delta
    return text, level


NO_BACKOFF = (0.0, 0.0)


class TestHappyPath:
    @pytest.mark.asyncio
    async def test_l0_success_no_retry(self):
        p = FakeProvider("m", ["好的"])
        text, level = await _collect(FallbackProvider(p, backoff_s=NO_BACKOFF))
        assert (text, level, p.calls) == ("好的", 0, 1)


class TestL1Retry:
    @pytest.mark.asyncio
    async def test_retries_same_model(self):
        p = FakeProvider("m", [LLMError("timeout"), "好的"])
        text, level = await _collect(FallbackProvider(p, backoff_s=NO_BACKOFF))
        assert (text, level, p.calls) == ("好的", 1, 2)

    @pytest.mark.asyncio
    async def test_respects_retry_count(self):
        p = FakeProvider("m", [LLMError("x")])
        with pytest.raises(LLMError):
            await _collect(FallbackProvider(p, retries=2, backoff_s=NO_BACKOFF))
        assert p.calls == 3, "应为 1 次原始 + 2 次重试"


class TestL2FallbackModel:
    @pytest.mark.asyncio
    async def test_switches_to_backup(self):
        primary = FakeProvider("m", [LLMError("x")])
        backup = FakeProvider("backup", ["备用回答"])
        text, level = await _collect(
            FallbackProvider(primary, fallback=backup, backoff_s=NO_BACKOFF),
        )
        assert (text, level) == ("备用回答", 2)
        assert primary.calls == 3
        assert backup.calls == 1

    @pytest.mark.asyncio
    async def test_skips_l2_when_no_backup_configured(self):
        """LLM_FALLBACK_MODEL 留空是允许的，不能因此崩。"""
        p = FakeProvider("m", [LLMError("x")])
        with pytest.raises(LLMError):
            await _collect(FallbackProvider(p, fallback=None, backoff_s=NO_BACKOFF))


class TestL3ShrinkTools:
    def _req_with_extra_tools(self) -> ChatRequest:
        names = CORE_TOOL_NAMES + ["extra_tool_a", "extra_tool_b"]
        return ChatRequest(messages=[], tools=[
            {"type": "function", "function": {"name": n}} for n in names
        ])

    @pytest.mark.asyncio
    async def test_shrinks_tool_set_and_retries(self):
        """失败不一定是模型挂了——十几个工具 schema 一起喂进去，
        小模型会选错工具或反复横跳。砍到核心几个常常就通了。"""
        p = FakeProvider("m", [LLMError("x"), LLMError("x"), LLMError("x"), "精简后成功"])
        text, level = await _collect(
            FallbackProvider(p, backoff_s=NO_BACKOFF), self._req_with_extra_tools(),
        )
        assert (text, level) == ("精简后成功", 3)
        names = [t["function"]["name"] for t in p.last_tools]
        assert "extra_tool_a" not in names
        assert set(names) <= set(CORE_TOOL_NAMES)

    @pytest.mark.asyncio
    async def test_skips_l3_when_no_tools(self):
        """纯对话请求没有工具可砍，不该白跑一次。"""
        p = FakeProvider("m", [LLMError("x")])
        with pytest.raises(LLMError):
            await _collect(FallbackProvider(p, backoff_s=NO_BACKOFF),
                           ChatRequest(messages=[], tools=[]))
        assert p.calls == 3, "无工具时不应触发 L3 的额外一次尝试"

    @pytest.mark.asyncio
    async def test_skips_l3_when_tools_already_minimal(self):
        """工具集本来就只有核心工具，砍了等于没砍，不该白跑。"""
        req = ChatRequest(messages=[], tools=[
            {"type": "function", "function": {"name": n}} for n in CORE_TOOL_NAMES
        ])
        p = FakeProvider("m", [LLMError("x")])
        with pytest.raises(LLMError):
            await _collect(FallbackProvider(p, backoff_s=NO_BACKOFF), req)
        assert p.calls == 3


class TestFatalErrors:
    @pytest.mark.asyncio
    async def test_fatal_not_retried(self):
        """401 重试一百次也还是 401，必须立刻上抛，不消耗重试预算。"""
        p = FakeProvider("m", [LLMFatalError("bad key")])
        with pytest.raises(LLMFatalError):
            await _collect(FallbackProvider(p, backoff_s=NO_BACKOFF))
        assert p.calls == 1

    @pytest.mark.asyncio
    async def test_fatal_on_backup_also_not_retried(self):
        primary = FakeProvider("m", [LLMError("x")])
        backup = FakeProvider("b", [LLMFatalError("bad key")])
        with pytest.raises(LLMFatalError):
            await _collect(FallbackProvider(primary, fallback=backup, backoff_s=NO_BACKOFF))
        assert backup.calls == 1


class TestExhaustion:
    @pytest.mark.asyncio
    async def test_all_levels_exhausted_raises_last_error(self):
        p = FakeProvider("m", [LLMError("最后的错误")])
        with pytest.raises(LLMError) as exc:
            await _collect(FallbackProvider(p, backoff_s=NO_BACKOFF))
        assert "最后的错误" in str(exc.value)

    @pytest.mark.asyncio
    async def test_core_tool_names_are_real_tools(self):
        """L3 缩到的核心工具必须真实存在，否则砍完一个工具都不剩。"""
        from app.core.tools.registry import load_tools, registry

        load_tools()
        for name in CORE_TOOL_NAMES:
            assert registry.has(name), f"CORE_TOOL_NAMES 里的 {name} 并未注册"
