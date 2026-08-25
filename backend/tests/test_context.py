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
