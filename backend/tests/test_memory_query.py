"""多轮指代的检索 query 构造。

L2 事实层的召回质量上限由 query 决定：库里存得再准，query 向量对不上也召不回。
多轮对话里用户的话天然残缺，这组测试盯的就是那个缺口。
"""

import pytest

from app.core.memory.query import (
    PER_MESSAGE_CHARS,
    RECENT_MESSAGES,
    build_context_query,
    is_referential,
)

HISTORY = [
    {"role": "user", "content": "帮我做个 8 周减脂计划"},
    {"role": "assistant", "content": "按你 82kg、TDEE 2770 算，每周减 0.6kg。"},
]


class TestIsReferential:
    @pytest.mark.parametrize("text", [
        "那午饭呢？",
        "那个计划再调一下",
        "改成 10 周",
        "按这个来",
        "刚才说的那份怎么执行",
        "上面提到的忌口还算吗",
        "再给我一个",
        "还有别的方案吗",
        "然后呢",
        "继续",
        "同样的思路做增力",
        "，那晚饭怎么办",
    ])
    def test_detects_dependence_on_prior_turns(self, text):
        assert is_referential(text), f"「{text}」离开上文无法理解，却被判为自足"

    @pytest.mark.parametrize("text", [
        "我右肩有旧伤，今天能练推举吗",
        "卧推 80kg 5x5",
        "帮我做个 8 周减脂计划",
        "办公室午饭怎么配",
        "我不吃香菜",
    ])
    def test_self_contained_text_needs_no_context(self, text):
        """自足的句子不能拼上文——掺进无关内容会把 query 向量拉偏，
        原本召得回的事实反而召不回。补召回不该以损失主召回为代价。"""
        assert not is_referential(text)

    def test_single_char_words_do_not_match_inside_sentence(self):
        """「还」「那」这类单字词按子串匹配会大面积误命中。"""
        assert not is_referential("把杠铃还回架子上算一组吗")
        assert not is_referential("深蹲和硬拉哪个更适合我")

    def test_empty_text_is_not_referential(self):
        assert not is_referential("")
        assert not is_referential("   ")


class TestBuildContextQuery:
    """返回的是一条**独立**的上文 query，不含当前句。

    拼接方案（上文 + 当前句合成一条）实测会稀释短句里的关键词，反而丢召回：
    「那午饭呢？」原句距离 0.479 召得回，拼上文变 0.641 召不回。详见
    core/memory/query.py 模块文档里的对比表。
    """

    def test_returns_context_without_current_text(self):
        query = build_context_query("那午饭呢？", HISTORY)
        assert query is not None
        assert "减脂计划" in query, "上文没有进入 query"
        assert "那午饭呢" not in query, (
            "当前句被拼进了上文 query。两路必须分开送——拼接会稀释短句关键词，"
            "实测把 0.479 的命中推到 0.641 的漏召"
        )

    def test_self_contained_text_returns_none(self):
        assert build_context_query("我右肩有旧伤，能练推举吗", HISTORY) is None

    def test_no_history_returns_none(self):
        assert build_context_query("那午饭呢？", []) is None

    def test_blank_history_returns_none(self):
        blank = [{"role": "user", "content": "  "}, {"role": "assistant", "content": ""}]
        assert build_context_query("那午饭呢？", blank) is None

    def test_takes_only_recent_messages(self):
        history = [
            {"role": "user", "content": "很久以前的无关话题：护腕怎么选"},
            *HISTORY,
        ]
        query = build_context_query("那午饭呢？", history)
        assert "护腕" not in query, "取了过多历史，早期无关内容会让上文 query 更糊"

    def test_recent_window_is_one_turn(self):
        """指代基本只指向最近一轮；取更多只会稀释。"""
        assert RECENT_MESSAGES == 2

    def test_long_message_is_truncated(self):
        """助理的计划说明可能上千字，整段送去检索会让向量糊成"健身话题"的
        平均值，跟每条具体事实都不近。"""
        history = [{"role": "assistant", "content": "细节" * 2000}]
        query = build_context_query("那午饭呢？", history)
        assert len(query) <= PER_MESSAGE_CHARS

    def test_result_is_deterministic(self):
        assert build_context_query("那午饭呢？", HISTORY) == build_context_query(
            "那午饭呢？", HISTORY,
        )
