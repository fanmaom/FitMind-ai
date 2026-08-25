"""读取用户记忆并生成场景化配餐。"""

from typing import Literal

from pydantic import BaseModel, Field
from sqlalchemy import select

from app.core.domain.macros import compute_macros
from app.core.domain.meal import FoodCandidate, build_meal, distribute_macros
from app.core.memory.facts import recall
from app.core.memory.profile import load_profile
from app.core.tools.registry import ToolContext, tool
from app.models.food import Food


class PlanMealsInput(BaseModel):
    tdee: float = Field(gt=0, le=10000, description="每日总消耗 kcal")
    weight_kg: float = Field(gt=0, le=500, description="当前体重 kg")
    meals: int = Field(default=3, ge=2, le=6, description="每日餐数")
    scenario: Literal["cook", "office", "eatout"] | None = Field(
        default=None, description="用餐场景；不填则自动读取用户档案",
    )


def _candidate(food: Food, scenario: str) -> FoodCandidate:
    if scenario == "cook":
        role = "protein" if food.category == "蛋白" else (
            "carb" if food.category == "主食" else "vegetable")
        category = "raw"
    else:
        role, category = "prepared", "ready" if scenario == "office" else "eatout"
    return FoodCandidate(food.name, food.kcal_per_100g, food.protein_g, food.carb_g,
                         food.fat_g, role, category)


@tool(
    name="plan_meals",
    description="按热量目标和居家、办公室、外食场景生成配餐。会自动读取档案中的忌口和用餐场景。",
    readonly=True,
)
async def plan_meals(inp: PlanMealsInput, ctx: ToolContext) -> dict:
    profile = await load_profile(ctx.session, ctx.user_id)
    scenarios = profile.get("meal_scenarios", {})
    scenario = inp.scenario or scenarios.get("default") or scenarios.get("weekday_lunch") or "cook"
    dislikes = profile.get("dislikes", [])
    recalled = await recall(ctx.session, ctx.user_id, f"{scenario} 用餐场景和忌口", k=3)

    if scenario == "cook":
        rows = (await ctx.session.scalars(select(Food).where(
            Food.category.in_(["蛋白", "主食", "蔬菜"])).limit(80))).all()
    else:
        rows = (await ctx.session.scalars(select(Food).where(Food.category == "外食").limit(80))).all()
    candidates = [_candidate(food, scenario) for food in rows]
    targets = distribute_macros(compute_macros(inp.tdee, "cut", inp.weight_kg), inp.meals)
    plans = [build_meal(scenario, target, candidates, dislikes) for target in targets]
    payload = {
        "scenario": scenario,
        "dislikes": dislikes,
        "memory_notes": [memory.content for memory in recalled],
        "meals": [{
            "meal": plan.target.meal,
            "target": {"kcal": plan.target.kcal, "protein_g": plan.target.protein_g,
                       "carb_g": plan.target.carb_g, "fat_g": plan.target.fat_g},
            "items": [item.__dict__ for item in plan.items],
            "preparation_note": plan.preparation_note,
        } for plan in plans],
    }
    return {
        "scenario": scenario, "meal_count": len(plans),
        "summary": f"已按 {scenario} 场景生成 {len(plans)} 餐，并自动避开 {len(dislikes)} 项忌口。",
        "__card__": {"type": "meal_plan", "payload": payload},
    }
