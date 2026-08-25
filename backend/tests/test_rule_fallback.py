"""L4 规则兜底。

核心断言：兜底给出的数字必须与正常模式逐位一致，因为走的是同一批
domain 纯函数。降级但仍可用，不是给个糊弄的答案。
"""

import pytest

from app.core.agent.rule_fallback import try_rule_fallback

PROFILE = {
    "weight_kg": 82.0, "height_cm": 178.0, "age": 30, "sex": "male",
    "activity": "moderate", "goal": "cut", "target_kg": 75.0,
}


class TestMacroIntent:
    @pytest.mark.parametrize("text", [
        "算一下我减脂该吃多少",
        "我每天应该摄入多少热量",
        "帮我算营养素",
        "三大营养素怎么分配",
        "蛋白质要吃多少",
        "一天多少大卡合适",
    ])
    def test_matches_macro_questions(self, text):
        r = try_rule_fallback(text, PROFILE)
        assert r.matched is True, f"未命中：{text}"
        assert r.card is not None
        assert r.card["type"] == "macros"

    def test_numbers_identical_to_normal_path(self):
        """这条是整个 L4 的意义所在：兜底不是给个糊弄的答案，
        而是给同一个正确答案——因为走的是同一批纯函数。"""
        from app.core.domain.energy import calc_bmr, calc_tdee
        from app.core.domain.macros import compute_macros

        expected = compute_macros(
            calc_tdee(calc_bmr(82.0, 178.0, 30, "male"), "moderate"), "cut", 82.0,
        )
        payload = try_rule_fallback("算一下我该吃多少", PROFILE).card["payload"]

        assert payload["protein_g"] == expected.protein_g
        assert payload["carb_g"] == expected.carb_g
        assert payload["fat_g"] == expected.fat_g
        assert payload["kcal"] == expected.kcal

    def test_text_states_degraded_mode(self):
        """必须让用户知道当前是降级模式，不能偷偷变笨。"""
        assert "不可用" in try_rule_fallback("该吃多少", PROFILE).text


class TestProjectionIntent:
    @pytest.mark.parametrize("text", ["我还要多久能到目标体重", "什么时候能到 75", "还要多少天"])
    def test_matches(self, text):
        r = try_rule_fallback(text, PROFILE)
        assert r.matched is True
        assert r.card["type"] == "projection"

    def test_picks_deficit_direction_for_cut(self):
        r = try_rule_fallback("还要多久", PROFILE)
        assert r.card["payload"]["weekly_change_kg"] > 0

    def test_picks_surplus_direction_for_bulk(self):
        bulking = {**PROFILE, "weight_kg": 70.0, "target_kg": 78.0}
        r = try_rule_fallback("还要多久", bulking)
        assert r.card["payload"]["weekly_change_kg"] < 0


class TestNoMatch:
    @pytest.mark.parametrize("text", [
        "今天心情不好", "帮我写个训练计划吧", "推荐几个背部动作",
        "深蹲要注意什么", "给我讲个笑话",
    ])
    def test_open_ended_goes_to_l5(self, text):
        """规则答不了的必须诚实返回未命中，不能假装回答。"""
        assert try_rule_fallback(text, PROFILE).matched is False


class TestIncompleteProfile:
    def test_missing_fields_do_not_crash(self):
        r = try_rule_fallback("算一下我该吃多少", {"weight_kg": 80})
        assert r.matched is True
        assert "档案不完整" in r.text
        assert r.card is None

    def test_lists_which_fields_are_missing(self):
        r = try_rule_fallback("算一下我该吃多少", {"weight_kg": 80, "height_cm": 178})
        for field in ("age", "sex", "activity", "goal"):
            assert field in r.text

    def test_empty_profile_does_not_crash(self):
        assert try_rule_fallback("该吃多少", {}).matched is True
