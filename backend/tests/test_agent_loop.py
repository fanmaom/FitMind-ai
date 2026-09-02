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


class TestFailureFeedbackDoesNotInviteLeaks:
    """失败反馈会原样进模型上下文，模型很容易把它整句转述给用户。
    所以反馈里说的是工具的用户可见说法，而且明写"别转述"。"""

    @pytest.mark.asyncio
    async def test_timeout_feedback_uses_label_not_internal_name(self, monkeypatch):
        import asyncio

        from app.core.tools import registry as reg_mod

        async def slow_invoke(name, args, ctx):
            await asyncio.sleep(10)

        monkeypatch.setattr(reg_mod.registry, "invoke", slow_invoke)

        p = ScriptedProvider([
            [ChatChunk(tool_calls=[ToolCall(id="c", name="log_workout", arguments={})],
                       finish_reason="tool_calls")],
            [ChatChunk(text_delta="换个方式"), ChatChunk(finish_reason="stop")],
        ])
        events = await _drain(_loop(p, tool_timeout_s=0.05))

        summary = next(e for e in events if e.type == "tool_result").data["summary"]
        assert "记录训练" in summary
        assert "log_workout" not in summary
        assert "不要" in summary, "没提醒模型别转述，它就会转述"

    @pytest.mark.asyncio
    async def test_exception_feedback_uses_label(self, monkeypatch):
        from app.core.tools import registry as reg_mod

        async def boom(name, args, ctx):
            raise RuntimeError("数据库连接断了")

        monkeypatch.setattr(reg_mod.registry, "invoke", boom)

        p = ScriptedProvider([
            [ChatChunk(tool_calls=[ToolCall(id="c", name="plan_meals", arguments={})],
                       finish_reason="tool_calls")],
            [ChatChunk(text_delta="稍后再试"), ChatChunk(finish_reason="stop")],
        ])
        events = await _drain(_loop(p))

        summary = next(e for e in events if e.type == "tool_result").data["summary"]
        assert "生成场景配餐" in summary
        assert "plan_meals" not in summary

    @pytest.mark.asyncio
    async def test_unknown_tool_keeps_the_hallucinated_name(self, monkeypatch):
        """模型编了个工具名时反过来——必须点出它编的那个名字，否则它改不过来。
        这条反馈不会到用户眼前：脱敏层拦在输出侧。"""
        from app.core.tools import registry as reg_mod
        from app.core.tools.registry import ToolNotFoundError

        async def missing(name, args, ctx):
            raise ToolNotFoundError(f"未注册的工具：{name}")

        monkeypatch.setattr(reg_mod.registry, "invoke", missing)

        p = ScriptedProvider([
            [ChatChunk(tool_calls=[ToolCall(id="c", name="set_reminder", arguments={})],
                       finish_reason="tool_calls")],
            [ChatChunk(text_delta="这个我做不到"), ChatChunk(finish_reason="stop")],
        ])
        events = await _drain(_loop(p))
        summary = next(e for e in events if e.type == "tool_result").data["summary"]
        assert "set_reminder" in summary

    @pytest.mark.asyncio
    async def test_tool_start_carries_label(self, monkeypatch):
        """前端状态行只能拿 label——拿到 name 就会显示"正在调用 plan_meals…"。"""
        from app.core.tools import registry as reg_mod

        async def ok(name, args, ctx):
            return {"ok": True}

        monkeypatch.setattr(reg_mod.registry, "invoke", ok)

        p = ScriptedProvider([
            [ChatChunk(tool_calls=[ToolCall(id="c", name="search_food", arguments={})],
                       finish_reason="tool_calls")],
            [ChatChunk(text_delta="查到了"), ChatChunk(finish_reason="stop")],
        ])
        events = await _drain(_loop(p))
        start = next(e for e in events if e.type == "tool_start")
        assert start.data["label"] == "查询食物营养"


class TestReasoningModelEmptyOutput:
    """线上真实故障：工具跑完（档案确实写进去了），回答却停在"先把你的档案建好"。

    原因是 hy3 把 max_tokens 全烧在思考上——实测工具结果回灌那一轮
    reasoning_tokens=2048/2048，正文 0 字，finish_reason=length，HTTP 200。
    """

    @pytest.mark.asyncio
    async def test_empty_turn_after_tools_is_retried_not_swallowed(self, monkeypatch):
        from app.core.tools import registry as reg_mod

        async def fake_invoke(name, args, ctx):
            return {"bmr": 2028.0, "tdee": 3143.4}

        monkeypatch.setattr(reg_mod.registry, "invoke", fake_invoke)

        p = ScriptedProvider([
            # 第 1 轮：一句铺垫 + 调工具
            [ChatChunk(text_delta="先把你的档案建好，同时算出你的代谢基线。"),
             ChatChunk(tool_calls=[ToolCall(id="c1", name="calc_energy_baseline", arguments={})],
                       finish_reason="tool_calls")],
            # 第 2 轮：思考预算烧光，正文空
            [ChatChunk(finish_reason="length", usage={"completion_tokens": 2048})],
            # 重试：正常出结果
            [ChatChunk(text_delta="基础代谢 2028 kcal，每日总消耗 3143 kcal。"),
             ChatChunk(finish_reason="stop")],
        ])
        events = await _drain(_loop(p))

        text = "".join(e.data["text"] for e in events if e.type == "text")
        assert "3143" in text, "空输出被当成说完了，用户拿不到结果"
        assert events[-1].type == "done"
        assert events[-1].data["degradation_level"] == 1, "重试过就该记为 L1"

    @pytest.mark.asyncio
    async def test_persistent_empty_output_degrades_instead_of_stopping_silently(self):
        """一直空就走 L4/L5：给个说法，而不是让用户对着半句话点重试。"""
        p = ScriptedProvider([[ChatChunk(finish_reason="length")]])
        events = await _drain(_loop(p), messages=[], profile={}, user_text="讲个笑话")
        assert events[-1].type == "error"
        assert events[-1].data["degradation_level"] == 5

    @pytest.mark.asyncio
    async def test_request_carries_the_configured_output_budget(self):
        """2048 不够这个模型思考完再说话，预算必须能配、且默认给够。"""
        from app.core.llm.client import DEFAULT_MAX_TOKENS

        seen: list[int] = []

        class Recording(ScriptedProvider):
            async def stream(self, req):
                seen.append(req.max_tokens)
                async for chunk in super().stream(req):
                    yield chunk

        p = Recording([[ChatChunk(text_delta="好"), ChatChunk(finish_reason="stop")]])
        await _drain(_loop(p, max_output_tokens=6000))
        assert seen == [6000]

        p2 = Recording([[ChatChunk(text_delta="好"), ChatChunk(finish_reason="stop")]])
        seen.clear()
        await _drain(_loop(p2))
        assert seen == [DEFAULT_MAX_TOKENS]
        assert DEFAULT_MAX_TOKENS >= 4096, "推理模型光思考就要两千多 token"


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
