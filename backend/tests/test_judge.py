"""判定调用的重试与空输出处理。

这个模块存在的唯一理由是推理模型会返回空字符串而不是报错，且思考长度有方差。
下面每条都对着那个失效模式。
"""

import pytest

from app.core.llm.judge import JudgeError, judge_json


class _FakeChunk:
    def __init__(self, text: str | None):
        self.text_delta = text


class _FakeProvider:
    """按脚本逐次返回不同输出，模拟"同一提示这次出结果下次出空"。"""

    def __init__(self, script: list[str | Exception]):
        self.script = list(script)
        self.calls = 0

    def stream(self, _request):
        self.calls += 1
        item = self.script.pop(0) if self.script else ""

        async def gen():
            if isinstance(item, Exception):
                raise item
            for piece in (item,):
                yield _FakeChunk(piece)

        return gen()


@pytest.fixture
def provider_script(monkeypatch):
    def install(script: list[str | Exception]) -> _FakeProvider:
        provider = _FakeProvider(script)
        monkeypatch.setattr(
            "app.core.llm.judge.build_provider", lambda *a, **k: provider,
        )
        return provider

    return install


class TestJudgeJson:
    @pytest.mark.asyncio
    async def test_parses_first_good_response(self, provider_script):
        provider = provider_script(['{"relation": "update"}'])
        assert await judge_json("p") == {"relation": "update"}
        assert provider.calls == 1

    @pytest.mark.asyncio
    async def test_retries_past_empty_output(self, provider_script):
        """核心场景：思考预算耗尽返回空串，重试就好了。"""
        provider = provider_script(["", "", '{"same": true}'])
        assert await judge_json("p") == {"same": True}
        assert provider.calls == 3

    @pytest.mark.asyncio
    async def test_retries_past_malformed_json(self, provider_script):
        provider = provider_script(["我觉得是同一件事", '{"same": false}'])
        assert await judge_json("p") == {"same": False}
        assert provider.calls == 2

    @pytest.mark.asyncio
    async def test_retries_past_transport_error(self, provider_script):
        provider = provider_script([RuntimeError("网关 502"), '{"same": true}'])
        assert await judge_json("p") == {"same": True}
        assert provider.calls == 2

    @pytest.mark.asyncio
    async def test_strips_code_fence(self, provider_script):
        provider_script(['```json\n{"same": true}\n```'])
        assert await judge_json("p") == {"same": True}

    @pytest.mark.asyncio
    async def test_raises_after_exhausting_attempts(self, provider_script):
        """必须抛，不能返回空 dict。

        静默返回 {} 会让调用方拿到默认值继续跑，功能退化成「从不判定」且没有
        任何报错——这正是 reconcile 之前踩的坑。
        """
        provider = provider_script(["", "", ""])
        with pytest.raises(JudgeError, match="3 次尝试"):
            await judge_json("p")
        assert provider.calls == 3

    @pytest.mark.asyncio
    async def test_rejects_non_object_json(self, provider_script):
        provider_script(["[1, 2, 3]", "[]", '"字符串"'])
        with pytest.raises(JudgeError):
            await judge_json("p")

    @pytest.mark.asyncio
    async def test_attempts_is_configurable(self, provider_script):
        provider = provider_script(["", ""])
        with pytest.raises(JudgeError):
            await judge_json("p", attempts=1)
        assert provider.calls == 1


class TestCallers:
    """两个判定点都必须走 judge_json，否则重试保护是空的。"""

    @pytest.mark.asyncio
    async def test_reconcile_judge_uses_shared_helper(self, monkeypatch):
        from app.core.memory.reconcile import _judge

        async def fake(_prompt, **_kwargs):
            return {"relation": "supersede"}

        monkeypatch.setattr("app.core.memory.reconcile.judge_json", fake)
        assert await _judge("旧", "新") == "supersede"

    @pytest.mark.asyncio
    async def test_reconcile_maps_unknown_relation_to_independent(self, monkeypatch):
        from app.core.memory.reconcile import _judge

        async def fake(_prompt, **_kwargs):
            return {"relation": "玄学"}

        monkeypatch.setattr("app.core.memory.reconcile.judge_json", fake)
        assert await _judge("旧", "新") == "independent"

    @pytest.mark.asyncio
    async def test_action_dedupe_uses_shared_helper(self, monkeypatch):
        from app.core.actions.dedupe import _is_same_task

        async def fake(_prompt, **_kwargs):
            return {"same": True}

        monkeypatch.setattr("app.core.actions.dedupe.judge_json", fake)
        assert await _is_same_task("旧", "新") is True

    @pytest.mark.asyncio
    async def test_action_dedupe_defaults_to_not_same(self, monkeypatch):
        """字段缺失时默认「不是重复」——宁可多留一条让用户自己划掉。"""
        from app.core.actions.dedupe import _is_same_task

        async def fake(_prompt, **_kwargs):
            return {}

        monkeypatch.setattr("app.core.actions.dedupe.judge_json", fake)
        assert await _is_same_task("旧", "新") is False
