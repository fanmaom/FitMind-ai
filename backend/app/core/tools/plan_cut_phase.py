"""生成并持久化减脂周期。"""

from pydantic import BaseModel, Field

from app.core.domain.macros import compute_macros
from app.core.domain.projection import KCAL_PER_KG_BODYWEIGHT, SAFE_WEEKLY_RATE
from app.core.tools.registry import ToolContext, tool
from app.models.plan import Plan


class PlanCutPhaseInput(BaseModel):
    tdee: float = Field(gt=0, le=10000, description="每日总消耗 kcal")
    weight_kg: float = Field(gt=0, le=500, description="当前体重 kg")
    weeks: int = Field(default=8, ge=1, le=52, description="减脂周期周数")
    carb_cycle: bool = Field(default=False, description="是否安排高、中、低碳循环")


def _daily_schedule(carb_g: float, carb_cycle: bool) -> list[dict]:
    factors = [1.0] * 7 if not carb_cycle else [1.2, 0.8, 1.0, 1.0, 0.8, 1.2, 1.0]
    labels = {1.2: "high", 1.0: "medium", 0.8: "low"}
    return [
        {"day": index + 1, "day_type": labels[factor], "carb_g": round(carb_g * factor, 1)}
        for index, factor in enumerate(factors)
    ]


@tool(
    name="plan_cut_phase",
    description="生成减脂周期与可选碳循环。用户要完整减脂安排时调用；返回摘要和 plan_id，明细放前端卡片。",
    readonly=False,
)
async def plan_cut_phase(inp: PlanCutPhaseInput, ctx: ToolContext) -> dict:
    macros = compute_macros(inp.tdee, "cut", inp.weight_kg)
    daily_target = {
        "kcal": macros.kcal, "protein_g": macros.protein_g,
        "carb_g": macros.carb_g, "fat_g": macros.fat_g,
    }
    weeks = [
        {"week": week, "days": _daily_schedule(macros.carb_g, inp.carb_cycle)}
        for week in range(1, inp.weeks + 1)
    ]
    payload = {
        "carb_cycle": inp.carb_cycle, "daily_target": daily_target, "weeks": weeks,
    }
    plan = Plan(user_id=ctx.user_id, type="cut", payload=payload, status="active")
    ctx.session.add(plan)
    await ctx.session.commit()

    weekly_change = round(macros.deficit_kcal * 7 / KCAL_PER_KG_BODYWEIGHT, 3)
    rate_note = (
        "速度过快，建议缩小热量差。"
        if abs(weekly_change) > inp.weight_kg * SAFE_WEEKLY_RATE
        else "预计变化速度在安全范围内。"
    )
    return {
        "plan_id": str(plan.id), "weeks": inp.weeks,
        "carb_cycle": inp.carb_cycle, "weekly_change_kg": weekly_change,
        "rate_note": rate_note,
        "summary": f"每日约 {macros.kcal:.0f} kcal，预计每周变化 {weekly_change:.2f} kg。",
        "__card__": {"type": "cut_plan", "payload": {"plan_id": str(plan.id), **payload}},
    }
