"""记录一次训练。"""

from datetime import date as date_type
import uuid

from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.api.v1.logs import build_idempotency_key
from app.core.tools.registry import ToolContext, tool
from app.models.workout_log import WorkoutLog
from app.models.plan import Plan


class WorkoutSetInput(BaseModel):
    weight: float = Field(ge=0, le=1000, description="该组重量（kg），自重动作填 0")
    reps: int = Field(ge=1, le=100, description="该组完成次数")
    rpe: float | None = Field(default=None, ge=1, le=10, description="自觉强度 1-10，可不填")


class LogWorkoutInput(BaseModel):
    date: date_type = Field(description="训练日期，格式 YYYY-MM-DD")
    exercise: str = Field(min_length=1, max_length=64, description="动作名，如「深蹲」「卧推」")
    sets: list[WorkoutSetInput] = Field(
        min_length=1, max_length=50, description="各组的重量与次数，一组一个元素",
    )
    plan_id: uuid.UUID | None = Field(default=None, description="关联的训练计划 ID，可不填")
    plan_week: int | None = Field(default=None, ge=1, le=52, description="计划周次，可不填")


@tool(
    name="log_workout",
    label="记录训练",
    description="记录一次训练的动作、重量与组数。用户描述了具体训练内容时调用。"
                "同一天同一动作同样内容重复调用不会重复写入。",
    readonly=False,
)
async def log_workout(inp: LogWorkoutInput, ctx: ToolContext) -> dict:
    plan_id = inp.plan_id
    if inp.plan_week is not None and plan_id is None:
        plan_id = await ctx.session.scalar(
            select(Plan.id).where(
                Plan.user_id == ctx.user_id, Plan.type == "strength", Plan.status == "active",
            ).order_by(Plan.created_at.desc()).limit(1),
        )
        if plan_id is None:
            return {"recorded": False, "note": "没有找到可关联的当前增力计划。"}
    if plan_id is not None:
        plan = await ctx.session.scalar(select(Plan).where(
            Plan.id == plan_id, Plan.user_id == ctx.user_id,
        ))
        if plan is None:
            return {"recorded": False, "note": "没有找到可关联的训练计划。"}
    sets = [s.model_dump() for s in inp.sets]
    key = build_idempotency_key(str(ctx.user_id), inp.date, inp.exercise, sets)

    stmt = (
        pg_insert(WorkoutLog)
        .values(user_id=ctx.user_id, date=inp.date, exercise=inp.exercise,
                sets=sets, idempotency_key=key, plan_id=plan_id, plan_week=inp.plan_week)
        .on_conflict_do_nothing(index_elements=["user_id", "idempotency_key"])
        .returning(WorkoutLog.id)
    )
    row = (await ctx.session.execute(stmt)).scalar_one_or_none()
    await ctx.session.commit()

    return {
        "id": str(row) if row else None,
        "deduplicated": row is None,
        "exercise": inp.exercise,
        "date": inp.date.isoformat(),
        "set_count": len(sets),
        "total_volume_kg": round(sum(s["weight"] * s["reps"] for s in sets), 1),
        "top_weight_kg": max(s["weight"] for s in sets),
    }
