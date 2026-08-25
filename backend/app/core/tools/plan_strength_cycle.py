"""生成并持久化增力周期。"""

from typing import Literal

from pydantic import BaseModel, Field

from app.core.domain.strength import WeekPlan, build_strength_cycle
from app.core.tools.registry import ToolContext, tool
from app.models.plan import Plan


class PlanStrengthCycleInput(BaseModel):
    lifts: dict[str, float] = Field(
        min_length=1,
        max_length=20,
        description="动作名到当前 1RM（kg）的映射，如 {\"深蹲\": 130}",
    )
    weeks: int = Field(default=8, ge=1, le=52, description="周期周数")
    scheme: Literal["linear", "5x5", "531"] = Field(description="递增方案")


def _serialize_week(plan: WeekPlan) -> dict:
    return {
        "week": plan.week,
        "is_deload": plan.is_deload,
        "lifts": {
            name: {
                "weight_kg": prescription.weight_kg,
                "sets": prescription.sets,
                "reps": prescription.reps,
                "intensity_pct": prescription.intensity_pct,
            }
            for name, prescription in plan.lifts.items()
        },
    }


@tool(
    name="plan_strength_cycle",
    description=(
        "生成增力周期计划。入参是各动作当前 1RM、周期长度、递增方案。"
        "返回摘要与 plan_id，完整周表已存库并推送到前端卡片；"
        "需要某一周明细时调用 get_plan_detail，不要让用户重新描述。"
    ),
    readonly=False,
)
async def plan_strength_cycle(inp: PlanStrengthCycleInput, ctx: ToolContext) -> dict:
    cycle = build_strength_cycle(inp.lifts, inp.weeks, inp.scheme)
    payload = {
        "scheme": inp.scheme,
        "weeks": [_serialize_week(week) for week in cycle],
    }
    plan = Plan(
        user_id=ctx.user_id,
        type="strength",
        payload=payload,
        status="active",
    )
    ctx.session.add(plan)
    await ctx.session.commit()

    first = cycle[0]
    # 卸载周不代表周期终点水平；摘要使用最后一个工作周，避免显示成倒退。
    last_working = next((week for week in reversed(cycle) if not week.is_deload), cycle[-1])
    summary = "；".join(
        f"{name} {first.lifts[name].weight_kg}kg → "
        f"{last_working.lifts[name].weight_kg}kg"
        for name in inp.lifts
    )
    return {
        "plan_id": str(plan.id),
        "scheme": inp.scheme,
        "weeks": inp.weeks,
        "deload_weeks": [week.week for week in cycle if week.is_deload],
        "summary": summary,
        "__card__": {
            "type": "strength_plan",
            "payload": {"plan_id": str(plan.id), **payload},
        },
    }
