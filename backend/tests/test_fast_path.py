"""结构化输入快路径。"""

import pytest

from app.core.agent.fast_path import try_fast_path

TODAY = "2026-08-25"


class TestWorkoutPatterns:
    @pytest.mark.parametrize("text,exercise,weight,sets,reps", [
        ("卧推 80kg 5x5", "卧推", 80.0, 5, 5),
        ("今天深蹲120kg 3组5次", "深蹲", 120.0, 3, 5),
        ("硬拉 140 kg 5×3", "硬拉", 140.0, 5, 3),
        ("引体向上 自重 4x8", "引体向上", 0.0, 4, 8),
        ("卧推 82.5公斤 3*8", "卧推", 82.5, 3, 8),
        ("过顶推举 50KG 4x6", "过顶推举", 50.0, 4, 6),
    ])
    def test_parses_common_formats(self, text, exercise, weight, sets, reps):
        hit = try_fast_path(text, TODAY)
        assert hit is not None, f"未命中：{text}"
        assert hit.tool_name == "log_workout"
        assert hit.arguments["exercise"] == exercise
        assert hit.arguments["date"] == TODAY
        assert len(hit.arguments["sets"]) == sets
        assert hit.arguments["sets"][0]["reps"] == reps
        assert hit.arguments["sets"][0]["weight"] == weight

    def test_expands_sets_into_individual_entries(self):
        hit = try_fast_path("深蹲 100kg 5x5", TODAY)
        assert len(hit.arguments["sets"]) == 5
        assert all(s == {"weight": 100.0, "reps": 5} for s in hit.arguments["sets"])


class TestBodyWeightPatterns:
    @pytest.mark.parametrize("text,kg", [
        ("今天体重81.6", 81.6),
        ("称了下 82kg", 82.0),
        ("体重 79.5 公斤", 79.5),
        ("体重：80", 80.0),
    ])
    def test_parses_body_weight(self, text, kg):
        hit = try_fast_path(text, TODAY)
        assert hit is not None, f"未命中：{text}"
        assert hit.tool_name == "log_body_metric"
        assert hit.arguments["weight_kg"] == kg


class TestNeverMisclassify:
    """宁可漏判走模型多花一次调用，不可误判把目标写进数据库。"""

    @pytest.mark.parametrize("text", [
        "我想减到75公斤",
        "目标体重 70kg",
        "打算把体重降到 68",
        "希望深蹲能到 140kg 5x5",
        "计划下周卧推 90kg 5x5",
        "争取体重到 75",
    ])
    def test_goal_statements_are_not_logged(self, text):
        assert try_fast_path(text, TODAY) is None, f"把目标误判成记录了：{text}"

    @pytest.mark.parametrize("text", [
        "今天该练什么",
        "帮我算一下热量",
        "上周深蹲做得不错",
        "深蹲要注意什么",
        "卧推怎么练",
        "我的体重正常吗",
    ])
    def test_ambiguous_input_goes_to_llm(self, text):
        assert try_fast_path(text, TODAY) is None, f"歧义输入被误命中：{text}"


class TestOutputMatchesToolSchema:
    """快路径直接调工具，参数必须能通过工具的 schema 校验，
    否则线上会以 ToolValidationError 的形式炸出来。"""

    @pytest.mark.parametrize("text", [
        "卧推 80kg 5x5", "引体向上 自重 4x8", "今天体重81.6",
    ])
    def test_arguments_validate_against_tool_schema(self, text):
        from app.core.tools.registry import load_tools, registry

        load_tools()
        hit = try_fast_path(text, TODAY)
        assert hit is not None
        spec = registry.get(hit.tool_name)
        spec.input_model.model_validate(hit.arguments)  # 不抛异常即通过
