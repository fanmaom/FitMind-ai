from datetime import date
import uuid

import pytest

from app.core.tools.registry import ToolContext, registry
from app.models.plan import Plan
from app.models.workout_log import WorkoutLog


async def _plan_and_log(db, user_id, reps=(5, 5, 5), rpes=(7, 8, 8)):
    plan = Plan(user_id=user_id, type="strength", status="active", payload={
        "scheme": "linear", "weeks": [
            {"week": 1, "lifts": {"深蹲": {"weight_kg": 100, "sets": 3, "reps": 5}}},
            {"week": 2, "lifts": {"深蹲": {"weight_kg": 105, "sets": 3, "reps": 5}}},
        ],
    })
    db.add(plan)
    await db.flush()
    db.add(WorkoutLog(
        user_id=user_id, date=date.today(), exercise="深蹲", plan_id=plan.id, plan_week=1,
        sets=[{"weight": 100, "reps": rep, "rpe": rpe} for rep, rpe in zip(reps, rpes)],
        idempotency_key=f"adapt-{plan.id}",
    ))
    await db.flush()
    return plan


@pytest.mark.asyncio
async def test_analysis_uses_linked_execution(db, seeded_user):
    plan = await _plan_and_log(db, seeded_user)
    out = await registry.invoke("analyze_plan_progress", {
        "plan_id": str(plan.id), "week": 1, "exercise": "深蹲",
    }, ctx=ToolContext(user_id=seeded_user, session=db))
    assert out["action"] == "increase"
    assert out["suggested_weight_kg"] == 102.5
    assert out["__card__"]["type"] == "plan_adjustment"


@pytest.mark.asyncio
async def test_confirmation_creates_version_without_overwriting_original(db, seeded_user):
    plan = await _plan_and_log(db, seeded_user)
    ctx = ToolContext(user_id=seeded_user, session=db)
    preview = await registry.invoke("adjust_strength_plan", {
        "plan_id": str(plan.id), "week": 1, "exercise": "深蹲",
    }, ctx=ctx)
    assert preview["needs_confirmation"] is True
    assert plan.status == "active"

    applied = await registry.invoke("adjust_strength_plan", {
        "plan_id": str(plan.id), "week": 1, "exercise": "深蹲", "confirmed": True,
    }, ctx=ctx)
    new = await db.get(Plan, uuid.UUID(applied["plan_id"]))
    await db.refresh(plan)
    assert plan.status == "superseded"
    assert plan.payload["weeks"][0]["lifts"]["深蹲"]["weight_kg"] == 100
    assert new.parent_plan_id == plan.id
    assert new.version == 2
    assert new.payload["weeks"][0]["lifts"]["深蹲"]["weight_kg"] == 102.5


@pytest.mark.asyncio
async def test_pain_never_auto_adjusts(db, seeded_user):
    plan = await _plan_and_log(db, seeded_user)
    out = await registry.invoke("adjust_strength_plan", {
        "plan_id": str(plan.id), "week": 1, "exercise": "深蹲", "pain": True,
        "confirmed": True,
    }, ctx=ToolContext(user_id=seeded_user, session=db))
    assert out["action"] == "stop"
    assert out["can_apply"] is False
    assert plan.status == "active"
