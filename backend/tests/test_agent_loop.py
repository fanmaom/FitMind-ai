"""Agent 主循环。"""

import pytest

from app.core.agent.loop import AgentLoop
from app.core.llm.client import ChatChunk, ChatRequest, LLMError, LLMFatalError, ToolCall
from app.core.llm.with_fallback import FallbackProvider


class ScriptedProvider:
    """按脚本逐轮返回 chunk 序列。每个元素是一轮的 chunk 列表或一个异常。"""

    def __init__(self, turns: list) -> None:
        self.model = "scripted"
        self.turns = turns
        self.calls = 0
        self.seen_messages: list[list[dict]] = []

    async def stream(self, req: ChatRequest):
        self.seen_messages.append(list(req.messages))
        outcome = self.turns[min(self.calls, len(self.turns) - 1)]
        self.calls += 1
        if isinstance(outcome, Exception):
            raise outcome
        for chunk in outcome:
            yield chunk


def _loop(provider, **kw) -> AgentLoop:
    return AgentLoop(FallbackProvider(provider, backoff_s=(0.0, 0.0)), tool_ctx=None, **kw)


async def _drain(loop: AgentLoop, **kwargs) -> list:
    base = {"messages": [{"role": "user", "content": "hi"}], "tools": [],
            "profile": {}, "user_text": "hi"}
    base.update(kwargs)
    return [e async for e in loop.run(**base)]


class TestPlainAnswer:
    @pytest.mark.asyncio
    async def test_ends_in_one_turn(self):
        p = ScriptedProvider([[ChatChunk(text_delta="你好"), ChatChunk(finish_reason="stop")]])
        events = await _drain(_loop(p))
        assert [e.type for e in events] == ["text", "done"]
        assert events[0].data["text"] == "你好"
        assert events[-1].data["truncated"] is False

    @pytest.mark.asyncio
    async def test_reports_usage(self):
        p = ScriptedProvider([[
            ChatChunk(text_delta="hi"),
            ChatChunk(finish_reason="stop", usage={"prompt_tokens": 100}),
        ]])
        events = await _drain(_loop(p))
        assert events[-1].data["usage"] == {"prompt_tokens": 100}


class TestToolCalls:
    @pytest.mark.asyncio
    async def test_executes_and_feeds_result_back(self, monkeypatch):
        from app.core.tools import registry as reg_mod

        async def fake_invoke(name, args, ctx):
            return {"kcal": 2100}

        monkeypatch.setattr(reg_mod.registry, "invoke", fake_invoke)

        p = ScriptedProvider([
            [ChatChunk(tool_calls=[ToolCall(id="c1", name="calc_macros", arguments={"tdee": 2600})],
                       finish_reason="tool_calls")],
            [ChatChunk(text_delta="每天 2100 大卡"), ChatChunk(finish_reason="stop")],
        ])
        events = await _drain(_loop(p))

        assert [e.type for e in events] == ["tool_start", "tool_result", "text", "done"]
        second = p.seen_messages[1]
        assert any(m.get("role") == "tool" for m in second), "工具结果没有回灌给模型"
        assert any(m.get("role") == "assistant" and m.get("tool_calls") for m in second)

    @pytest.mark.asyncio
    async def test_parallel_tool_calls_all_executed(self, monkeypatch):
        from app.core.tools import registry as reg_mod

        seen = []

        async def fake_invoke(name, args, ctx):
            seen.append(name)
            return {"ok": True}

        monkeypatch.setattr(reg_mod.registry, "invoke", fake_invoke)

        p = ScriptedProvider([
            [ChatChunk(tool_calls=[
                ToolCall(id="a", name="t1", arguments={}),
                ToolCall(id="b", name="t2", arguments={}),
            ], finish_reason="tool_calls")],
            [ChatChunk(text_delta="好了"), ChatChunk(finish_reason="stop")],
        ])
        await _drain(_loop(p))
        assert seen == ["t1", "t2"]

    @pytest.mark.asyncio
    async def test_invalid_args_fed_back_not_raised(self, monkeypatch):
        """参数校验失败不是异常，是给模型的反馈——让它自己改。"""
        from app.core.tools import registry as reg_mod
        from app.core.tools.registry import ToolValidationError

        calls = {"n": 0}

        async def fake_invoke(name, args, ctx):
            calls["n"] += 1
            if calls["n"] == 1:
                raise ToolValidationError("参数校验失败 —— weight_kg: 必须大于 0（收到 -5）")
            return {"ok": True}

        monkeypatch.setattr(reg_mod.registry, "invoke", fake_invoke)

        p = ScriptedProvider([
            [ChatChunk(tool_calls=[ToolCall(id="c1", name="t", arguments={"weight_kg": -5})],
                       finish_reason="tool_calls")],
            [ChatChunk(tool_calls=[ToolCall(id="c2", name="t", arguments={"weight_kg": 80})],
                       finish_reason="tool_calls")],
            [ChatChunk(text_delta="好了"), ChatChunk(finish_reason="stop")],
        ])
        events = await _drain(_loop(p))

        assert events[-1].type == "done"
        tool_msgs = [m for m in p.seen_messages[1] if m.get("role") == "tool"]
        assert any("weight_kg" in m["content"] for m in tool_msgs)

    @pytest.mark.asyncio
    async def test_tool_timeout_becomes_feedback(self, monkeypatch):
        import asyncio

        from app.core.tools import registry as reg_mod

        async def slow_invoke(name, args, ctx):
            await asyncio.sleep(10)

        monkeypatch.setattr(reg_mod.registry, "invoke", slow_invoke)

        p = ScriptedProvider([
            [ChatChunk(tool_calls=[ToolCall(id="c", name="slow", arguments={})],
                       finish_reason="tool_calls")],
            [ChatChunk(text_delta="换个方式"), ChatChunk(finish_reason="stop")],
        ])
        events = await _drain(_loop(p, tool_timeout_s=0.05))

        result = next(e for e in events if e.type == "tool_result")
        assert "超时" in result.data["summary"]
        assert events[-1].type == "done", "工具超时不该让整轮失败"

    @pytest.mark.asyncio
    async def test_tool_exception_becomes_feedback(self, monkeypatch):
        from app.core.tools import registry as reg_mod

        async def boom(name, args, ctx):
            raise RuntimeError("数据库连接断了")

        monkeypatch.setattr(reg_mod.registry, "invoke", boom)

        p = ScriptedProvider([
            [ChatChunk(tool_calls=[ToolCall(id="c", name="t", arguments={})],
                       finish_reason="tool_calls")],
            [ChatChunk(text_delta="稍后再试"), ChatChunk(finish_reason="stop")],
        ])
        events = await _drain(_loop(p))
        assert events[-1].type == "done"


class TestLoopBounds:
    @pytest.mark.asyncio
    async def test_max_turns_truncates_instead_of_looping_forever(self, monkeypatch):
        from app.core.tools import registry as reg_mod

        async def fake_invoke(name, args, ctx):
            return {"again": True}

        monkeypatch.setattr(reg_mod.registry, "invoke", fake_invoke)

        forever = [ChatChunk(tool_calls=[ToolCall(id="c", name="t", arguments={})],
                             finish_reason="tool_calls")]
        p = ScriptedProvider([forever])
        events = await _drain(_loop(p, max_turns=3))

        assert p.calls == 3
        assert events[-1].type == "done"
        assert events[-1].data["truncated"] is True


class TestDegradation:
    PROFILE = {"weight_kg": 82.0, "height_cm": 178.0, "age": 30,
               "sex": "male", "activity": "moderate", "goal": "cut"}

    @pytest.mark.asyncio
    async def test_falls_through_to_l4_rule_fallback(self):
        """L1-L3 全挂之后走 L4：完全绕过 LLM，仍然给出正确数字。"""
        p = ScriptedProvider([LLMError("all dead")])
        events = await _drain(_loop(p), messages=[], profile=self.PROFILE,
                              user_text="算一下我该吃多少")

        card = next(e for e in events if e.type == "card")
        assert card.data["type"] == "macros"
        assert events[-1].data["degradation_level"] == 4

    @pytest.mark.asyncio
    async def test_l4_numbers_match_normal_path(self):
        """兜底不是给个糊弄的答案，是给同一个正确答案。"""
        from app.core.domain.energy import calc_bmr, calc_tdee
        from app.core.domain.macros import compute_macros

        expected = compute_macros(
            calc_tdee(calc_bmr(82.0, 178.0, 30, "male"), "moderate"), "cut", 82.0,
        )
        p = ScriptedProvider([LLMError("dead")])
        events = await _drain(_loop(p), messages=[], profile=self.PROFILE,
                              user_text="该吃多少")
        payload = next(e for e in events if e.type == "card").data["payload"]
        assert payload["protein_g"] == expected.protein_g
        assert payload["kcal"] == expected.kcal

    @pytest.mark.asyncio
    async def test_l5_honest_failure_when_rules_miss(self):
        """L4 也答不了就诚实失败，不能假装回答。"""
        p = ScriptedProvider([LLMError("all dead")])
        events = await _drain(_loop(p), messages=[], profile={}, user_text="给我讲个笑话")

        assert events[-1].type == "error"
        assert events[-1].data["degradation_level"] == 5
        assert "已经保存" in events[-1].data["message"]

    @pytest.mark.asyncio
    async def test_fatal_error_also_reaches_l4(self):
        """key 配错也该走兜底，而不是把 500 抛给用户。"""
        p = ScriptedProvider([LLMFatalError("bad key")])
        events = await _drain(_loop(p), messages=[], profile=self.PROFILE,
                              user_text="该吃多少")
        assert events[-1].data["degradation_level"] == 4


class TestProviderUnavailable:
    """provider 连构造都失败（最常见是 LLM 未配置）是"LLM 不可用"最极端的
    形式，必须走 L4 兜底，而不是把构造异常变成 L5。"""

    PROFILE = {"weight_kg": 82.0, "height_cm": 178.0, "age": 30,
               "sex": "male", "activity": "moderate", "goal": "cut"}

    @pytest.mark.asyncio
    async def test_none_provider_goes_to_l4(self):
        loop = AgentLoop(None, tool_ctx=None)
        events = [e async for e in loop.run(
            messages=[], tools=[], profile=self.PROFILE, user_text="我该吃多少",
        )]
        assert next(e for e in events if e.type == "card").data["type"] == "macros"
        assert events[-1].data["degradation_level"] == 4

    @pytest.mark.asyncio
    async def test_none_provider_still_honest_when_rules_miss(self):
        loop = AgentLoop(None, tool_ctx=None)
        events = [e async for e in loop.run(
            messages=[], tools=[], profile={}, user_text="讲个笑话",
        )]
        assert events[-1].type == "error"
        assert events[-1].data["degradation_level"] == 5
