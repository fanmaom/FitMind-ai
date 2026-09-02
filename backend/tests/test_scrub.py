"""流式文本脱敏：内部标识符不允许出现在用户可见的回复里。

这层是兜底而不是主手段——主手段是系统提示里的硬性规则。但提示只是"要求"，
模型不保证遵守，而"绝不泄漏"是产品承诺，得有一层确定性代码来兑现。

难点全在流式：`log_workout` 会被切成任意几段吐出来，逐段替换必然漏。
所以下面大量用例是关于**切分**的。
"""

import pytest

from app.core.agent.scrub import TextScrubber, build_vocab

VOCAB = {"log_workout": "记录训练", "plan_meals": "生成场景配餐", "weight_kg": "当前体重"}


def _stream(chunks: list[str], vocab: dict | None = None) -> str:
    """按给定切分喂进去，返回拼接后的完整输出。"""
    s = TextScrubber(vocab or VOCAB)
    return "".join(s.feed(c) for c in chunks) + s.flush()


class TestReplacement:
    def test_replaces_tool_name_with_label(self):
        assert _stream(["我可以用 log_workout 帮你记"]) == "我可以用 「记录训练」 帮你记"

    def test_swallows_wrapping_backticks(self):
        """反引号留在原地会变成裸的 `「记录训练」`——前端是纯文本渲染，不吃 Markdown。"""
        assert _stream(["——`log_workout` 只能存做过的训练"]) == \
            "——「记录训练」 只能存做过的训练"

    def test_replaces_profile_field_name(self):
        assert _stream(["缺少 weight_kg，补齐再算"]) == "缺少 「当前体重」，补齐再算"

    def test_replaces_every_occurrence(self):
        out = _stream(["先 log_workout，再 plan_meals，最后 log_workout"])
        assert "log_workout" not in out
        assert out.count("「记录训练」") == 2

    def test_chinese_text_untouched(self):
        text = "基础代谢 1931 kcal，每日总消耗 2993 kcal。蛋白 202g。"
        assert _stream([text]) == text

    def test_partial_word_not_replaced(self):
        """`log_workout_v2` 不是 `log_workout`，边界判断错了会切出半截标识符。"""
        assert _stream(["字段 log_workout_v2 未知"]) == "字段 log_workout_v2 未知"
        assert _stream(["xlog_workout 不是它"]) == "xlog_workout 不是它"

    def test_cjk_neighbours_count_as_boundary(self):
        r"""中文是 \w，所以不能用 \b 判边界——必须显式排除 ASCII 标识符字符。"""
        assert _stream(["用log_workout记"]) == "用「记录训练」记"


class TestStreamingSplits:
    @pytest.mark.parametrize("cut", range(1, 11))
    def test_identifier_split_at_any_offset(self, cut):
        """逐字节切都得替换掉。这是这个类唯一存在的理由。"""
        name = "log_workout"
        out = _stream(["用 " + name[:cut], name[cut:] + " 记一下"])
        assert out == "用 「记录训练」 记一下"

    def test_split_one_char_at_a_time(self):
        text = "用 `log_workout` 存训练"
        assert _stream(list(text)) == "用 「记录训练」 存训练"

    def test_backtick_arriving_alone_is_held(self):
        """反引号先到、标识符后到：反引号不能先发出去，否则收不回来了。"""
        assert _stream(["见 `", "log_workout`"]) == "见 「记录训练」"

    def test_nothing_is_lost(self):
        chunks = ["今天", "练了 ", "深蹲", " 120kg", "，", "已记录"]
        assert _stream(chunks) == "".join(chunks)

    def test_flush_emits_held_tail(self):
        """回复以标识符结尾时，尾巴还在缓冲里——不 flush 就等于吞掉一段话。"""
        s = TextScrubber(VOCAB)
        assert s.feed("看 log_workout") == "看 ", "标识符可能没写完，不能先发出去"
        assert s.flush() == "「记录训练」"

    def test_flush_is_idempotent(self):
        s = TextScrubber(VOCAB)
        assert s.feed("已记录") == "已记录", "中文结尾没有歧义，不该被扣住"
        assert s.flush() == ""
        assert s.flush() == ""

    def test_trailing_ascii_word_released_on_next_chunk(self):
        """英文词会被短暂扣住等边界，但下一个 chunk 一到就必须放出来。"""
        s = TextScrubber(VOCAB)
        s.feed("总消耗 2993 kcal")
        assert "kcal" in s.feed("，赤字 500。")

    def test_empty_chunks_are_safe(self):
        assert _stream(["", "已记录", ""]) == "已记录"


class TestInterruptedMidIdentifier:
    """用户点停止时流可能断在标识符中间。那半截替不掉——词表里没有 log_work——
    原样放出去就是泄漏。"""

    def test_half_written_identifier_is_dropped(self):
        s = TextScrubber(VOCAB)
        assert s.feed("这条得用 log_work") == "这条得用 "
        assert s.flush() == ""

    def test_backtick_plus_half_identifier_is_dropped(self):
        s = TextScrubber(VOCAB)
        s.feed("见 `log_wor")
        assert s.flush() == ""

    def test_complete_identifier_is_still_replaced(self):
        s = TextScrubber(VOCAB)
        s.feed("这条得用 log_workout")
        assert s.flush() == "「记录训练」"

    def test_plain_english_word_is_not_dropped(self):
        """carb 是 carb_g 的前缀，但它是个写完的词——丢掉就吞了用户该看的字。"""
        s = TextScrubber({"carb_g": "碳水"})
        assert s.feed("多吃点 carb") == "多吃点 "
        assert s.flush() == "carb"


class TestUnknownIdentifiers:
    @staticmethod
    def _warnings(chunks: list[str]) -> list[str]:
        """loguru 不走 stdlib logging，caplog 抓不到，得自己挂个 sink。"""
        from app.core.logger import logger

        captured: list[str] = []
        sink = logger.add(lambda m: captured.append(str(m)), level="WARNING")
        try:
            _stream(chunks)
        finally:
            logger.remove(sink)
        return captured

    def test_unknown_snake_case_is_left_intact(self):
        """词表外的标识符不猜着替换——替错比露出来更糟。只记日志，靠日志补词表。"""
        assert _stream(["字段 some_new_field 不认识"]) == "字段 some_new_field 不认识"

    def test_unknown_snake_case_is_logged(self):
        lines = self._warnings(["字段 some_new_field 不认识"])
        assert any("some_new_field" in line for line in lines)

    def test_same_identifier_logged_once(self):
        lines = self._warnings(["a some_new_field b some_new_field c some_new_field"])
        assert sum("some_new_field" in line for line in lines) == 1


class TestDefaultVocab:
    def test_covers_every_registered_tool(self):
        """新增工具零改动就该自动进词表，否则总有一天漏一个。"""
        from app.core.tools.registry import registry

        vocab = build_vocab()
        for spec in registry.all():
            assert vocab.get(spec.name) == spec.label, f"{spec.name} 不在词表里"

    def test_covers_every_profile_field(self):
        from app.core.memory.profile import PROFILE_FIELDS

        vocab = build_vocab()
        for key in PROFILE_FIELDS:
            assert key in vocab, f"档案字段 {key} 不在词表里"

    def test_labels_carry_no_internal_identifiers(self):
        """替换文本里再出现 snake_case 就是白替了。"""
        for key, label in build_vocab().items():
            assert "_" not in label, f"{key} 的替换文本 {label!r} 仍带下划线"

    def test_real_leak_from_production_is_scrubbed(self):
        """线上真实泄漏样本（库里捞出来的原句）。"""
        leaked = "如果想让我排一个恢复训练计划（用 `plan_strength_cycle` 或下肢保护性安排）"
        s = TextScrubber()
        out = "".join(s.feed(ch) for ch in leaked) + s.flush()
        assert "plan_strength_cycle" not in out
        assert "「编排增力周期」" in out

    def test_plan_id_is_scrubbed(self):
        out = _stream(["已生成 8 周减脂计划（plan_id 见卡片）"], build_vocab())
        assert "plan_id" not in out

    def test_enum_values_are_scrubbed(self):
        """另一条线上真实泄漏。枚举值没法只靠输入侧堵：工具 Schema 必须把值域
        交给模型，模型才填得对参数，所以它一定认识 office 和 cook。"""
        leaked = "要不要我按你的用餐场景（周中日间 office、其余 cook）出一份减脂配餐？"
        out = _stream([leaked], build_vocab())
        assert "office" not in out
        assert "cook" not in out
        assert "带饭或公司简餐" in out

    def test_capitalised_english_is_not_touched(self):
        """枚举值一律小写。英文行文里的 Office / Cut 不该被当成内部值替换。"""
        text = "Office 这个词本身没问题，Cut 也一样。"
        assert _stream([text], build_vocab()) == text
