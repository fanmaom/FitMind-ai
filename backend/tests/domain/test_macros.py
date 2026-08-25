"""三大营养素分配。"""

import pytest

from app.core.domain.macros import KCAL_PER_GRAM, compute_macros


# 刻意写死而不引用 KCAL_PER_GRAM。用被测模块自己的常数算期望值，
# 常数一改两边一起改，测试就永远抓不到错的常数（变异测试验证过）。
# 4/4/9 是 Atwater 系数，是营养学事实，不是本项目的实现选择。
ATWATER_PROTEIN = 4
ATWATER_CARB = 4
ATWATER_FAT = 9


def _total_kcal(r) -> float:
    return (r.protein_g * ATWATER_PROTEIN
            + r.carb_g * ATWATER_CARB
            + r.fat_g * ATWATER_FAT)


class TestConstants:
    """把 Atwater 系数钉死。写错一个，所有热量计算全线偏差。"""

    def test_atwater_factors_are_correct(self):
        assert KCAL_PER_GRAM["protein"] == ATWATER_PROTEIN
        assert KCAL_PER_GRAM["carb"] == ATWATER_CARB
        assert KCAL_PER_GRAM["fat"] == ATWATER_FAT


class TestGoalDirection:
    def test_cut_creates_deficit(self):
        r = compute_macros(tdee=2600, goal="cut", weight_kg=80)
        assert r.deficit_kcal == pytest.approx(-500, abs=1.0)
        assert r.kcal == pytest.approx(2100, abs=1.0)

    def test_bulk_creates_surplus(self):
        r = compute_macros(tdee=2600, goal="bulk", weight_kg=80)
        assert r.deficit_kcal == pytest.approx(300, abs=1.0)

    def test_maintain_is_neutral(self):
        r = compute_macros(tdee=2600, goal="maintain", weight_kg=80)
        assert r.deficit_kcal == pytest.approx(0, abs=1.0)


class TestInvariants:
    @pytest.mark.parametrize("goal", ["cut", "bulk", "maintain"])
    def test_macros_sum_back_to_kcal(self, goal):
        """三大营养素换算回热量必须与 kcal 字段一致——本模块的核心不变式。"""
        r = compute_macros(2600, goal, 80)
        assert _total_kcal(r) == pytest.approx(r.kcal, abs=0.5), f"{goal} 热量对不上"

    @pytest.mark.parametrize("tdee,weight", [
        (1500, 95), (1400, 100), (3500, 60), (2000, 50), (4000, 120),
    ])
    def test_never_negative_across_extremes(self, tdee, weight):
        """低 TDEE + 大体重时碳水会被挤成负数，必须先压脂肪保底。"""
        r = compute_macros(tdee=tdee, goal="cut", weight_kg=weight)
        assert r.carb_g >= 0, f"碳水为负：{r}"
        assert r.fat_g > 0
        assert r.protein_g > 0
        assert _total_kcal(r) == pytest.approx(r.kcal, abs=0.5)

    def test_deficit_never_exceeds_forty_percent(self):
        """极端赤字不安全，要有下限保护。"""
        r = compute_macros(tdee=2000, goal="cut", weight_kg=50)
        assert r.kcal >= 2000 * 0.6


class TestProtein:
    def test_cut_uses_higher_protein(self):
        """减脂期蛋白拉高以保住瘦体重。"""
        cut = compute_macros(2600, "cut", 80)
        bulk = compute_macros(2600, "bulk", 80)
        assert cut.protein_g > bulk.protein_g
        assert cut.protein_g == pytest.approx(80 * 2.2)

    def test_protein_scales_with_bodyweight(self):
        light = compute_macros(2600, "cut", 60)
        heavy = compute_macros(2600, "cut", 90)
        assert heavy.protein_g > light.protein_g


class TestValidation:
    @pytest.mark.parametrize("tdee,goal,weight", [
        (0, "cut", 80), (-100, "cut", 80), (2600, "cut", 0),
        (2600, "cut", -80), (2600, "unknown", 80),
    ])
    def test_rejects_invalid_input(self, tdee, goal, weight):
        with pytest.raises(ValueError):
            compute_macros(tdee, goal, weight)

    def test_error_message_names_field_and_value(self):
        with pytest.raises(ValueError) as exc:
            compute_macros(2600, "cut", -80)
        assert "weight_kg" in str(exc.value)
