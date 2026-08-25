"""基础代谢与每日总消耗。"""

import pytest

from app.core.domain.energy import ACTIVITY_FACTORS, calc_bmr, calc_tdee


class TestCalcBMR:
    """Mifflin-St Jeor 公式。"""

    def test_male_reference_value(self):
        # 10*80 + 6.25*178 - 5*30 + 5 = 800 + 1112.5 - 150 + 5 = 1767.5
        assert calc_bmr(weight_kg=80, height_cm=178, age=30, sex="male") == pytest.approx(1767.5)

    def test_female_reference_value(self):
        # 10*60 + 6.25*165 - 5*28 - 161 = 600 + 1031.25 - 140 - 161 = 1330.25
        assert calc_bmr(weight_kg=60, height_cm=165, age=28, sex="female") == pytest.approx(1330.25)

    def test_male_higher_than_female_same_body(self):
        assert calc_bmr(70, 175, 30, "male") - calc_bmr(70, 175, 30, "female") == pytest.approx(166.0)

    def test_heavier_means_higher_bmr(self):
        assert calc_bmr(90, 175, 30, "male") > calc_bmr(70, 175, 30, "male")

    def test_older_means_lower_bmr(self):
        assert calc_bmr(70, 175, 50, "male") < calc_bmr(70, 175, 30, "male")

    @pytest.mark.parametrize("kwargs", [
        {"weight_kg": 0, "height_cm": 175, "age": 30, "sex": "male"},
        {"weight_kg": -5, "height_cm": 175, "age": 30, "sex": "male"},
        {"weight_kg": 70, "height_cm": 0, "age": 30, "sex": "male"},
        {"weight_kg": 70, "height_cm": 175, "age": 0, "sex": "male"},
        {"weight_kg": 70, "height_cm": 175, "age": 130, "sex": "male"},
        {"weight_kg": 70, "height_cm": 175, "age": 30, "sex": "other"},
    ])
    def test_rejects_invalid_input(self, kwargs):
        with pytest.raises(ValueError):
            calc_bmr(**kwargs)

    def test_error_message_names_the_field_and_value(self):
        """报错文本会被回灌给模型让它自行纠正，必须点名字段和收到的值。"""
        with pytest.raises(ValueError) as exc:
            calc_bmr(weight_kg=-5, height_cm=175, age=30, sex="male")
        assert "weight_kg" in str(exc.value)
        assert "-5" in str(exc.value)


class TestCalcTDEE:
    def test_sedentary(self):
        assert calc_tdee(1700, "sedentary") == pytest.approx(1700 * 1.2)

    def test_monotonic_across_levels(self):
        levels = ["sedentary", "light", "moderate", "active", "very_active"]
        values = [calc_tdee(1700, lv) for lv in levels]
        assert values == sorted(values), "活动量越大 TDEE 必须越高"

    def test_all_levels_have_factor(self):
        assert set(ACTIVITY_FACTORS) == {
            "sedentary", "light", "moderate", "active", "very_active",
        }

    def test_tdee_always_above_bmr(self):
        for level in ACTIVITY_FACTORS:
            assert calc_tdee(1700, level) > 1700

    @pytest.mark.parametrize("bmr,level", [(0, "moderate"), (-100, "moderate"), (1700, "superhuman")])
    def test_rejects_invalid_input(self, bmr, level):
        with pytest.raises(ValueError):
            calc_tdee(bmr, level)
