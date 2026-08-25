"""记录一次训练。"""

from datetime import date as date_type

from pydantic import BaseModel, Field
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.api.v1.logs import build_idempotency_key
from app.core.tools.registry import ToolContext, tool
from app.models.workout_log import WorkoutLog


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


@tool(
    name="log_workout",
    description="记录一次训练的动作、重量与组数。用户描述了具体训练内容时调用。"
                "同一天同一动作同样内容重复调用不会重复写入。",
    readonly=False,
)
async def log_workout(inp: LogWorkoutInput, ctx: ToolContext) -> dict:
    sets = [s.model_dump() for s in inp.sets]
    key = build_idempotency_key(str(ctx.user_id), inp.date, inp.exercise, sets)

    stmt = (
        pg_insert(WorkoutLog)
        .values(user_id=ctx.user_id, date=inp.date, exercise=inp.exercise,
                sets=sets, idempotency_key=key)
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
