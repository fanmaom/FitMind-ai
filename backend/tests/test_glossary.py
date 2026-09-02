"""术语表：内部标识符 → 人话。

档案字段名、枚举值这些东西同时出现在提示、工具反馈、降级文案、流式脱敏和
前端状态行里。翻译分散在各处必然对不齐，所以统一在这里定义一次。
"""

import pytest

from app.core.agent.glossary import enum_label, field_label, render_value


class TestFieldLabel:
    @pytest.mark.parametrize(("key", "expected"), [
        ("weight_kg", "当前体重"),
        ("height_cm", "身高"),
        ("age", "年龄"),
        ("sex", "生理性别"),
        ("activity", "日常活动量"),
        ("goal", "当前目标"),
        ("target_kg", "目标体重"),
        ("injuries", "伤病列表"),
        ("meal_scenarios", "各餐场景"),
    ])
    def test_translates_known_fields(self, key, expected):
        assert field_label(key) == expected

    def test_drops_unit_and_enum_annotations(self):
        """PROFILE_FIELDS 的说明是写给模型看的（含单位、取值域），
        直接拿来给用户看会带出 male / female 这类枚举。"""
        for key in ("sex", "activity", "goal", "phase_started_on", "lifts"):
            label = field_label(key)
            assert "：" not in label
            assert "（" not in label
            assert "_" not in label

    def test_unknown_key_returns_itself(self):
        """兜底不能返回空串——宁可露出字段名，也不能把话说漏一截。"""
        assert field_label("no_such_field") == "no_such_field"

    def test_covers_all_profile_fields(self):
        from app.core.memory.profile import PROFILE_FIELDS

        for key in PROFILE_FIELDS:
            assert field_label(key) != key, f"{key} 没有中文名"


class TestEnumLabel:
    @pytest.mark.parametrize(("value", "expected"), [
        ("cut", "减脂"), ("bulk", "增肌"), ("maintain", "维持"),
        ("male", "男"), ("female", "女"),
        ("cook", "自己做"), ("office", "带饭或公司简餐"), ("eatout", "外食"),
        ("sedentary", "久坐"), ("very_active", "高强度活动"),
    ])
    def test_translates_known_values(self, value, expected):
        assert enum_label(value) == expected

    def test_passes_through_unknown_and_non_string(self):
        assert enum_label("健身房") == "健身房"
        assert enum_label(82.5) == "82.5"

    def test_covers_every_enum_the_tools_accept(self):
        """工具签名里的 Literal 就是模型会写回来的值域。漏一个就漏一个泄漏点。"""
        from typing import get_args

        from app.core.tools.plan_meals import PlanMealsInput

        annotation = PlanMealsInput.model_fields["scenario"].annotation
        scenarios = [
            v for arg in get_args(annotation) for v in get_args(arg) if isinstance(v, str)
        ]
        assert scenarios, "没取到 Literal 值域，这条测试本身失效了"
        for value in scenarios:
            assert enum_label(value) != value, f"{value} 没有中文名"


class TestRenderValue:
    def test_scalar(self):
        assert render_value(82.0) == "82.0"

    def test_enum_string(self):
        assert render_value("cut") == "减脂"

    def test_list_joined(self):
        assert render_value(["右肩", "右踝"]) == "右肩、右踝"

    def test_dict_uses_slot_labels(self):
        out = render_value({"weekday_lunch": "office"})
        assert "weekday_lunch" not in out
        assert "带饭或公司简餐" in out

    def test_dict_passes_through_free_form_keys(self):
        """lifts 的键是动作名，模型自己写的中文，不该被当成内部字段处理。"""
        assert render_value({"深蹲": 130}) == "深蹲 130"

    def test_dict_is_key_sorted(self):
        """档案每轮注入提示，序列化顺序不稳定会打断缓存前缀。"""
        assert render_value({"b": 1, "a": 2}) == render_value({"a": 2, "b": 1})

    def test_empty_containers(self):
        assert render_value([]) == "无"
        assert render_value({}) == "无"

    def test_none(self):
        assert render_value(None) == "无"
