"""上下文组装与 Token 预算。"""

import pytest

from app.core.agent.context import (
    KEEP_RECENT_TURNS,
    build_context,
    compress_history,
    estimate_tokens,
)
from app.core.agent.prompts import SYSTEM_PROMPT

PROFILE = {"weight_kg": 82, "height_cm": 178, "goal": "cut", "injuries": ["右肩"]}


def _joined(messages: list[dict]) -> str:
    return "".join(m["content"] for m in messages if isinstance(m.get("content"), str))


class TestCacheFriendlyOrdering:
    """排序即缓存策略。前缀变一个字节，后面全部失效，而且没有任何报错。"""

    def test_system_prompt_is_first(self):
        msgs, _ = build_context(profile=PROFILE, facts=[], history=[],
                                user_text="你好", today="2026-08-25")
        assert msgs[0]["role"] == "system"
        assert msgs[0]["content"] == SYSTEM_PROMPT

    def test_system_prompt_contains_no_volatile_content(self):
        """日期、用户数据写进系统提示会让缓存前缀每轮不同。"""
        assert "2026" not in SYSTEM_PROMPT
        assert "今天" not in SYSTEM_PROMPT
        assert "82" not in SYSTEM_PROMPT

    def test_system_prompt_identical_across_users(self):
        a, _ = build_context(profile=PROFILE, facts=[], history=[],
                             user_text="x", today="2026-08-25")
        b, _ = build_context(profile={"weight_kg": 60}, facts=["别的事实"], history=[],
                             user_text="y", today="2026-08-26")
        assert a[0] == b[0], "系统提示在不同用户间必须逐字节相同"

    def test_date_appears_after_system_prompt(self):
        msgs, _ = build_context(profile=PROFILE, facts=[], history=[],
                                user_text="你好", today="2026-08-25")
        assert "2026-08-25" in _joined(msgs[1:])

    def test_user_text_is_last(self):
        msgs, _ = build_context(profile=PROFILE, facts=[], history=[],
                                user_text="今天练什么", today="2026-08-25")
        assert msgs[-1] == {"role": "user", "content": "今天练什么"}

    def test_profile_serialization_is_deterministic(self):
        """dict 序列化顺序不稳定会让缓存失效——必须 sort_keys。"""
        a, _ = build_context(profile={"b": 2, "a": 1}, facts=[], history=[],
                             user_text="x", today="2026-08-25")
        b, _ = build_context(profile={"a": 1, "b": 2}, facts=[], history=[],
                             user_text="x", today="2026-08-25")
        assert a == b


class TestContent:
    def test_profile_and_facts_included(self):
        msgs, _ = build_context(profile=PROFILE, facts=["公司楼下有轻食店"],
                                history=[], user_text="午饭吃啥", today="2026-08-25")
        joined = _joined(msgs)
        assert "右肩" in joined
        assert "公司楼下有轻食店" in joined

    def test_empty_profile_prompts_for_onboarding(self):
        msgs, _ = build_context(profile={}, facts=[], history=[],
                                user_text="你好", today="2026-08-25")
        assert "暂无" in _joined(msgs)

    def test_history_is_included_in_order(self):
        history = [
            {"role": "user", "content": "第一句"},
            {"role": "assistant", "content": "第一答"},
        ]
        msgs, _ = build_context(profile={}, facts=[], history=history,
                                user_text="第二句", today="2026-08-25")
        assert msgs[-3:] == history + [{"role": "user", "content": "第二句"}]


class TestNoInternalIdentifiers:
    """注入提示的内容会被模型照抄进回复。档案里带一个 weight_kg，
    用户就有机会在回答里看到 weight_kg——线上真实发生过。"""

    FULL = {
        "weight_kg": 82.0, "height_cm": 178.0, "age": 30, "sex": "male",
        "activity": "moderate", "goal": "cut", "target_kg": 75.0,
        "injuries": ["右踝"], "meal_scenarios": {"weekday_lunch": "office"},
        "lifts": {"深蹲": 130},
    }

    def _profile_block(self, profile: dict) -> str:
        msgs, _ = build_context(profile=profile, facts=[], history=[],
                                user_text="x", today="2026-08-25")
        return _joined(msgs[1:])

    def test_system_prompt_forbids_leaking_internals(self):
        assert "工具名" in SYSTEM_PROMPT, "系统提示里没有禁止暴露内部实现的规则"

    def test_profile_carries_no_field_names(self):
        block = self._profile_block(self.FULL)
        for key in self.FULL:
            assert key not in block, f"档案块里出现内部字段名 {key}"

    def test_profile_carries_no_enum_values(self):
        block = self._profile_block(self.FULL)
        for raw in ("male", "moderate", "cut", "office", "weekday_lunch"):
            assert raw not in block, f"档案块里出现内部枚举值 {raw}"

    def test_profile_uses_chinese_labels_and_keeps_units(self):
        block = self._profile_block(self.FULL)
        assert "当前体重" in block
        assert "减脂" in block
        assert "cm" in block, "单位得留给模型，否则 178 是什么它得猜"

    def test_profile_keeps_free_form_values(self):
        block = self._profile_block(self.FULL)
        assert "右踝" in block
        assert "深蹲" in block

    def test_profile_rendering_is_stable(self):
        """档案每轮注入，渲染不稳定会打断缓存前缀。"""
        assert self._profile_block(self.FULL) == self._profile_block(dict(self.FULL))


class TestNoDuplicateCurrentMessage:
    """本轮的用户消息在调用 build_context 之前就已经落库了（先落库再推流是
    有意的），所以它会被 load_history 正常取到。不排除的话，同一句话会在
    prompt 里出现两遍：一次在历史里，一次在末尾。

    这个 bug 不报错也不影响功能，只表现为白烧 token，以及模型偶尔把用户的话
    当成说了两遍来回应——正是那种能在线上活很久的问题。
    """

    @pytest.mark.asyncio
    async def test_current_user_message_excluded_from_history(self, db, seeded_user):
        from app.models.conversation import Conversation
        from app.models.message import Message
        from app.services.chat_service import load_history

        conv = Conversation(user_id=seeded_user, title="t")
        db.add(conv)
        await db.flush()

        for text in ("上一轮的问题", "上一轮的回答"):
            db.add(Message(
                conversation_id=conv.id, user_id=seeded_user,
                role="user" if "问题" in text else "assistant",
                content={"text": text}, status="done",
            ))
        current = Message(
            conversation_id=conv.id, user_id=seeded_user, role="user",
            content={"text": "本轮的问题"}, status="done",
        )
        db.add(current)
        await db.flush()

        full = await load_history(db, conv.id)
        assert "本轮的问题" in [m["content"] for m in full]

        trimmed = await load_history(db, conv.id, exclude_id=current.id)
        assert [m["content"] for m in trimmed] == ["上一轮的问题", "上一轮的回答"]

    @pytest.mark.asyncio
    async def test_prompt_contains_current_message_exactly_once(self, db, seeded_user):
        from app.models.conversation import Conversation
        from app.models.message import Message
        from app.services.chat_service import load_history

        conv = Conversation(user_id=seeded_user, title="t")
        db.add(conv)
        await db.flush()
        current = Message(
            conversation_id=conv.id, user_id=seeded_user, role="user",
            content={"text": "帮我算一下今天的营养素"}, status="done",
        )
        db.add(current)
        await db.flush()

        history = await load_history(db, conv.id, exclude_id=current.id)
        msgs, _ = build_context(
            profile={}, facts=[], history=history,
            user_text="帮我算一下今天的营养素", today="2026-08-25",
        )
        assert _joined(msgs).count("帮我算一下今天的营养素") == 1


class TestBudget:
    def test_reports_each_segment(self):
        _, budget = build_context(profile=PROFILE, facts=["a", "b"], history=[],
                                  user_text="你好", today="2026-08-25")
        assert budget.system > 0
        assert budget.profile > 0
        assert budget.facts > 0
        assert budget.total >= budget.system + budget.profile + budget.facts

    def test_memory_overhead_stays_small(self):
        """档案 + 事实的记忆开销要控制在几百 token，否则记忆系统就成了负担。"""
        _, budget = build_context(
            profile=PROFILE, facts=[f"事实{i}" for i in range(5)],
            history=[], user_text="你好", today="2026-08-25",
        )
        assert budget.profile + budget.facts < 800


class TestCompressHistory:
    def test_keeps_recent_turns_verbatim(self):
        history = [{"role": "user", "content": f"消息{i}"} for i in range(20)]
        out = compress_history(history, keep_recent=6, max_tokens=10_000)
        assert out[-6:] == history[-6:]

    def test_drops_oldest_when_over_budget(self):
        history = [{"role": "user", "content": "很长的内容" * 200} for _ in range(20)]
        out = compress_history(history, keep_recent=6, max_tokens=500)
        assert len(out) < len(history)
        assert out[-6:] == history[-6:], "最近 6 轮无论如何都要保留"

    def test_recent_turns_kept_even_if_over_budget(self):
        """短期连贯性优先——最近几轮再长也不能丢，否则指代解析不了。"""
        history = [{"role": "user", "content": "超长" * 5000} for _ in range(8)]
        out = compress_history(history, keep_recent=6, max_tokens=100)
        assert len(out) == 6

    def test_shorter_than_window_returned_intact(self):
        history = [{"role": "user", "content": "a"}, {"role": "assistant", "content": "b"}]
        assert compress_history(history, keep_recent=6, max_tokens=500) == history

    def test_empty_history_is_safe(self):
        assert compress_history([], keep_recent=6, max_tokens=500) == []

    def test_default_window_matches_config(self):
        assert KEEP_RECENT_TURNS == 6


class TestEstimateTokens:
    def test_chinese_roughly_one_per_char(self):
        assert 8 <= estimate_tokens("这是一段中文测试文本") <= 12

    def test_english_roughly_quarter_per_char(self):
        assert estimate_tokens("a" * 400) <= 120

    def test_empty_is_zero(self):
        assert estimate_tokens("") == 0
