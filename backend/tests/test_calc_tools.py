"""计算类工具。

这四个工具只做一件事：把 domain 纯函数包装成模型可调用的形式。
业务逻辑的正确性已在 tests/domain/ 覆盖，这里只验包装层——
参数校验、字段透传、报错可回灌。
"""

import pytest

from app.core.tools.registry import ToolValidationError, load_tools, registry


@pytest.fixture(scope="module", autouse=True)
def _load():
    load_tools()


class TestCalcEnergyBaseline:
    @pytest.mark.asyncio
    async def test_returns_bmr_and_tdee(self):
        out = await registry.invoke("calc_energy_baseline", {
            "weight_kg": 80, "height_cm": 178, "age": 30, "sex": "male", "activity": "moderate",
        }, ctx=None)
        assert out["bmr"] == pytest.approx(1767.5)
        assert out["tdee"] == pytest.approx(1767.5 * 1.55, abs=0.1)

    @pytest.mark.asyncio
    async def test_rejects_negative_weight_with_readable_error(self):
        with pytest.raises(ToolValidationError) as exc:
            await registry.invoke("calc_energy_baseline", {
                "weight_kg": -5, "height_cm": 178, "age": 30, "sex": "male", "activity": "moderate",
            }, ctx=None)
        assert "weight_kg" in str(exc.value)

    @pytest.mark.asyncio
    async def test_rejects_unknown_activity_level(self):
        with pytest.raises(ToolValidationError) as exc:
            await registry.invoke("calc_energy_baseline", {
                "weight_kg": 80, "height_cm": 178, "age": 30, "sex": "male", "activity": "superhuman",
            }, ctx=None)
        assert "activity" in str(exc.value)

    @pytest.mark.asyncio
    async def test_rejects_absurd_height(self):
        """300cm 的身高一定是模型填错了单位，拦下来比算出个荒谬结果强。"""
        with pytest.raises(ToolValidationError):
            await registry.invoke("calc_energy_baseline", {
                "weight_kg": 80, "height_cm": 1780, "age": 30, "sex": "male", "activity": "moderate",
            }, ctx=None)


class TestCalcMacros:
    @pytest.mark.asyncio
    async def test_returns_full_breakdown(self):
        out = await registry.invoke("calc_macros", {
            "tdee": 2600, "goal": "cut", "weight_kg": 80,
        }, ctx=None)
        assert set(out) == {
            "kcal", "protein_g", "carb_g", "fat_g", "deficit_kcal",
            "weekly_change_kg", "rate_note",
        }
        assert out["protein_g"] == pytest.approx(176.0)
        assert out["kcal"] < 2600

    @pytest.mark.asyncio
    async def test_returns_weekly_rate_and_safety_note(self):
        out = await registry.invoke("calc_macros", {
            "tdee": 2770.6, "goal": "cut", "weight_kg": 82,
        }, ctx=None)
        assert out["weekly_change_kg"] == pytest.approx(-0.45, abs=0.02)
        assert out["rate_note"]

    @pytest.mark.asyncio
    async def test_rejects_unknown_goal(self):
        with pytest.raises(ToolValidationError) as exc:
            await registry.invoke("calc_macros", {
                "tdee": 2600, "goal": "shred", "weight_kg": 80,
            }, ctx=None)
        assert "goal" in str(exc.value)


class TestEstimateOneRm:
    @pytest.mark.asyncio
    async def test_epley(self):
        out = await registry.invoke("estimate_1rm", {"weight_kg": 100, "reps": 5}, ctx=None)
        assert out["one_rm"] == pytest.approx(116.67, abs=0.01)

    @pytest.mark.asyncio
    async def test_rejects_reps_beyond_formula_validity(self):
        with pytest.raises(ToolValidationError):
            await registry.invoke("estimate_1rm", {"weight_kg": 100, "reps": 50}, ctx=None)


class TestProjectGoal:
    @pytest.mark.asyncio
    async def test_projects_time_to_target(self):
        out = await registry.invoke("project_goal", {
            "current_kg": 82, "target_kg": 75, "weekly_deficit_kcal": 3500,
        }, ctx=None)
        assert out["feasible"] is True
        assert out["weeks"] == pytest.approx(15.4, abs=0.1)

    @pytest.mark.asyncio
    async def test_flags_infeasible_direction(self):
        out = await registry.invoke("project_goal", {
            "current_kg": 82, "target_kg": 75, "weekly_deficit_kcal": -2000,
        }, ctx=None)
        assert out["feasible"] is False
        assert "盈余" in out["note"]

    @pytest.mark.asyncio
    async def test_flags_unsafe_rate(self):
        out = await registry.invoke("project_goal", {
            "current_kg": 82, "target_kg": 75, "weekly_deficit_kcal": 10000,
        }, ctx=None)
        assert "过快" in out["note"]


class TestCalcToolsAreReadonly:
    """计算类工具无副作用，必须标 readonly——降级层据此决定能否安全重试。"""

    @pytest.mark.parametrize("name", [
        "calc_energy_baseline", "calc_macros", "estimate_1rm", "project_goal",
    ])
    def test_marked_readonly(self, name):
        assert registry.get(name).readonly is True


class TestToolDescriptions:
    """描述是模型决定何时调用的唯一依据，质量直接决定工具能不能被用对。"""

    @pytest.mark.parametrize("name", [
        "calc_energy_baseline", "calc_macros", "estimate_1rm", "project_goal",
    ])
    def test_description_states_when_to_use(self, name):
        desc = registry.get(name).description
        assert len(desc) >= 40, f"{name} 的描述太短，模型判断不了何时该调"
        assert "时调用" in desc or "调用本工具" in desc, f"{name} 的描述没说清何时使用"

    @pytest.mark.parametrize("name", [
        "calc_energy_baseline", "calc_macros", "estimate_1rm", "project_goal",
    ])
    def test_every_field_has_description(self, name):
        props = registry.get(name).input_model.model_json_schema()["properties"]
        for field, schema in props.items():
            assert schema.get("description"), f"{name}.{field} 缺字段描述"
