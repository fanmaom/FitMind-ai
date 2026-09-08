"""比较增力计划与实际训练，给出确定性的调整建议。"""

import uuid

from pydantic import BaseModel, Field
from sqlalchemy import select

from app.core.domain.training_adjustment import analyze_training_feedback, round_to_increment
from app.core.tools.registry import ToolContext, tool
from app.models.plan import Plan
from app.models.workout_log import WorkoutLog


class AnalyzePlanProgressInput(BaseModel):
    plan_id: uuid.UUID | None = Field(default=None, description="计划 ID；未提供时使用最新生效的增力计划")
    week: int = Field(ge=1, le=52)
    exercise: str = Field(min_length=1, max_length=64)
    pain: bool = Field(default=False, description="用户是否报告疼痛")


async def build_progress(inp: AnalyzePlanProgressInput, ctx: ToolContext) -> dict:
    stmt = select(Plan).where(Plan.user_id == ctx.user_id, Plan.type == "strength")
    if inp.plan_id is not None:
        stmt = stmt.where(Plan.id == inp.plan_id)
    else:
        stmt = stmt.where(Plan.status == "active").order_by(Plan.created_at.desc()).limit(1)
    plan = await ctx.session.scalar(stmt)
    if plan is None or plan.type != "strength":
        return {"found": False, "note": "未找到对应的增力计划。"}
    week = next((w for w in plan.payload.get("weeks", []) if w.get("week") == inp.week), None)
    target = (week or {}).get("lifts", {}).get(inp.exercise)
    if target is None:
        return {"found": False, "note": "该周计划中没有这个动作。"}
    rows = list((await ctx.session.scalars(
        select(WorkoutLog).where(
            WorkoutLog.user_id == ctx.user_id,
            WorkoutLog.plan_id == plan.id,
            WorkoutLog.plan_week == inp.week,
            WorkoutLog.exercise == inp.exercise,
        ).order_by(WorkoutLog.date.desc()).limit(2),
    )).all())
    if not rows:
        return {"found": False, "note": "还没有与该计划关联的训练记录。"}

    planned_sets, planned_reps = int(target["sets"]), int(target["reps"])
    def failed(row: WorkoutLog) -> bool:
        return sum(s["reps"] for s in row.sets[:planned_sets]) < planned_sets * planned_reps * 0.8
    consecutive = 2 if len(rows) >= 2 and all(failed(row) for row in rows[:2]) else int(failed(rows[0]))
    latest = rows[0]
    rpes = [float(s["rpe"]) for s in latest.sets if s.get("rpe") is not None]
    feedback = analyze_training_feedback(
        planned_sets=planned_sets, planned_reps=planned_reps,
        completed_reps=[int(s["reps"]) for s in latest.sets], rpes=rpes,
        consecutive_failures=consecutive, pain=inp.pain,
    )
    suggested = round_to_increment(float(target["weight_kg"]) * feedback.load_multiplier)
    return {
        "found": True, "plan_id": str(plan.id), "week": inp.week, "exercise": inp.exercise,
        "target_weight_kg": target["weight_kg"], "suggested_weight_kg": suggested,
        "completion_rate": feedback.completion_rate, "average_rpe": feedback.average_rpe,
        "action": feedback.action, "reason": feedback.reason,
        "can_apply": feedback.action not in {"stop", "hold"},
    }


@tool(
    name="analyze_plan_progress", label="分析计划完成情况",
    description="根据已关联的训练记录、完成率和 RPE 分析增力计划是否需要升重、维持、降载或卸载。",
    readonly=True,
)
async def analyze_plan_progress(inp: AnalyzePlanProgressInput, ctx: ToolContext) -> dict:
    result = await build_progress(inp, ctx)
    if result.get("found"):
        result["__card__"] = {"type": "plan_adjustment", "payload": result.copy()}
    return result
