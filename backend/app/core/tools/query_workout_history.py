"""查询某动作的训练历史（返回聚合值）。"""

from datetime import date as date_type, timedelta

from pydantic import BaseModel, Field
from sqlalchemy import select

from app.core.tools.registry import ToolContext, tool
from app.models.workout_log import WorkoutLog


class QueryWorkoutHistoryInput(BaseModel):
    exercise: str = Field(min_length=1, max_length=64, description="动作名，如「深蹲」")
    weeks: int = Field(default=8, ge=1, le=104, description="回溯多少周，默认 8 周")


def _volume(sets: list[dict]) -> float:
    return sum(s["weight"] * s["reps"] for s in sets)


def _top_weight(sets: list[dict]) -> float:
    return max((s["weight"] for s in sets), default=0.0)


@tool(
    name="query_workout_history",
    label="查询训练记录",
    description="查询某个动作近期的训练进展，返回训练次数、起止重量、容量变化等聚合指标。"
                "回答「我深蹲进步了多少」「最近练得怎么样」这类问题时调用。",
    readonly=True,
)
async def query_workout_history(inp: QueryWorkoutHistoryInput, ctx: ToolContext) -> dict:
    since = date_type.today() - timedelta(weeks=inp.weeks)
    rows = (await ctx.session.scalars(
        select(WorkoutLog)
        .where(WorkoutLog.exercise == inp.exercise, WorkoutLog.date >= since)
        .order_by(WorkoutLog.date),
    )).all()

    if not rows:
        # 查无数据要说清楚，让模型换个说法回复，而不是重试——
        # 重试一百次也还是查无数据。
        return {
            "exercise": inp.exercise, "weeks": inp.weeks, "session_count": 0,
            "note": f"最近 {inp.weeks} 周没有「{inp.exercise}」的训练记录。",
        }

    first, last = rows[0], rows[-1]
    v0, v1 = _volume(first.sets), _volume(last.sets)
    latest_rpe = max((s.get("rpe") or 0) for s in last.sets) or None

    # 返回聚合值而非原始行：几百条记录塞进上下文既爆 Token 又让模型
    # 不得不自己做算术——而模型做算术会错。
    return {
        "exercise": inp.exercise,
        "weeks": inp.weeks,
        "session_count": len(rows),
        "first_date": first.date.isoformat(),
        "latest_date": last.date.isoformat(),
        "start_weight_kg": _top_weight(first.sets),
        "latest_weight_kg": _top_weight(last.sets),
        "start_volume_kg": round(v0, 1),
        "latest_volume_kg": round(v1, 1),
        "volume_change_pct": round((v1 - v0) / v0 * 100, 1) if v0 else 0.0,
        "best_weight_kg": max(_top_weight(r.sets) for r in rows),
        "latest_rpe": latest_rpe,
        "note": "",
    }
