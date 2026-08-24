"""日志层路由（L3）。"""

import hashlib
import json
from datetime import date as date_type

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.api.deps import RequestContext, get_context
from app.models.workout_log import WorkoutLog

router = APIRouter(prefix="/api/v1/logs", tags=["logs"])


class WorkoutSet(BaseModel):
    weight: float = Field(ge=0, le=1000, description="该组重量（kg），自重动作填 0")
    reps: int = Field(ge=1, le=100)
    rpe: float | None = Field(default=None, ge=1, le=10)


class WorkoutCreate(BaseModel):
    date: date_type
    exercise: str = Field(min_length=1, max_length=64)
    sets: list[WorkoutSet] = Field(min_length=1, max_length=50)


def build_idempotency_key(user_id: str, day: date_type, exercise: str, sets: list[dict]) -> str:
    """同一用户、同一天、同一动作、同样组数 → 同一把键。

    重试机制遇上写操作必须幂等，否则网络抖一下就把一次训练记成两条，
    后续所有趋势分析全部被污染。
    """
    raw = json.dumps(
        {"u": user_id, "d": day.isoformat(), "e": exercise, "s": sets},
        sort_keys=True, ensure_ascii=False,
    )
    return hashlib.sha256(raw.encode()).hexdigest()[:64]


@router.post("/workouts", status_code=201)
async def create_workout(
    payload: WorkoutCreate, ctx: RequestContext = Depends(get_context),
) -> dict:
    sets = [s.model_dump() for s in payload.sets]
    key = build_idempotency_key(str(ctx.user_id), payload.date, payload.exercise, sets)

    stmt = (
        pg_insert(WorkoutLog)
        .values(user_id=ctx.user_id, date=payload.date, exercise=payload.exercise,
                sets=sets, idempotency_key=key)
        .on_conflict_do_nothing(index_elements=["user_id", "idempotency_key"])
        .returning(WorkoutLog.id)
    )
    row = (await ctx.session.execute(stmt)).scalar_one_or_none()
    await ctx.session.commit()
    return {"id": str(row) if row else None, "deduplicated": row is None}


@router.get("/workouts")
async def list_workouts(ctx: RequestContext = Depends(get_context)) -> list[dict]:
    rows = await ctx.session.scalars(select(WorkoutLog).order_by(WorkoutLog.date.desc()))
    return [
        {"id": str(r.id), "date": r.date.isoformat(), "exercise": r.exercise, "sets": r.sets}
        for r in rows
    ]
