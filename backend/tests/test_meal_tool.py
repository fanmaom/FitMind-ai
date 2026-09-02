"""配餐工具与记忆接缝。"""

import pytest

from app.core.memory.profile import merge_profile
from app.core.tools.registry import ToolContext, load_tools, registry


@pytest.fixture(scope="module", autouse=True)
def _load():
    load_tools()


@pytest.mark.asyncio
async def test_tool_uses_profile_scenario_and_dislikes(db, seeded_user, monkeypatch):
    import app.core.tools.plan_meals as module
    from scripts.seed_foods import seed

    async def no_recall(*_args, **_kwargs):
        return []

    monkeypatch.setattr(module, "recall", no_recall)
    await seed()
    await merge_profile(db, seeded_user, {
        "dislikes": ["牛肉"], "meal_scenarios": {"default": "office"},
    })
    out = await registry.invoke("plan_meals", {
        "tdee": 2500, "weight_kg": 75, "meals": 3,
    }, ctx=ToolContext(user_id=seeded_user, session=db))
    assert out["scenario"] == "office"
    assert out["__card__"]["payload"]["dislikes"] == ["牛肉"]
    assert all(item["category"] == "ready"
               for meal in out["__card__"]["payload"]["meals"] for item in meal["items"])


@pytest.mark.asyncio
async def test_summary_for_the_model_carries_no_raw_enum(db, seeded_user, monkeypatch):
    """summary 是写给模型看的一句话，模型基本会照抄——线上就抄出过
    「周中日间 office、其余 cook」。结构化字段留原值，句子里用人话。"""
    import app.core.tools.plan_meals as module
    from scripts.seed_foods import seed

    async def no_recall(*_args, **_kwargs):
        return []

    monkeypatch.setattr(module, "recall", no_recall)
    await seed()
    out = await registry.invoke("plan_meals", {
        "tdee": 2500, "weight_kg": 75, "meals": 3, "scenario": "office",
    }, ctx=ToolContext(user_id=seeded_user, session=db))

    assert "office" not in out["summary"]
    assert "带饭或公司简餐" in out["summary"]
    assert out["scenario"] == "office", "结构化字段仍是原始枚举，前端卡片按它渲染"
