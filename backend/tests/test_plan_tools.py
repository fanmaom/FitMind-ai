"""增力周期计划工具。"""

import json
import uuid

import pytest

from app.core.agent.loop import AgentLoop
from app.core.tools.registry import ToolContext, load_tools, registry
from app.models.plan import Plan


@pytest.fixture(scope="module", autouse=True)
def _load_tools():
    load_tools()


INPUT = {
    "lifts": {"深蹲": 130.0, "卧推": 95.0},
    "weeks": 8,
    "scheme": "linear",
}


class TestPlanStrengthCycle:
    @pytest.mark.asyncio
    async def test_model_receives_summary_not_full_matrix(self, db, seeded_user):
        ctx = ToolContext(user_id=seeded_user, session=db)
        loop = AgentLoop(None, ctx)
        result_text, card = await loop._execute_tool("plan_strength_cycle", INPUT)

        result = json.loads(result_text)
        assert "plan_id" in result
        assert not isinstance(result.get("weeks"), list)
        assert len(result_text) < 600, f"模型返回体过大（{len(result_text)} 字符）"
        assert card["type"] == "strength_plan"
        assert len(card["payload"]["weeks"]) == 8

    @pytest.mark.asyncio
    async def test_full_matrix_persisted(self, db, seeded_user):
        ctx = ToolContext(user_id=seeded_user, session=db)
        out = await registry.invoke("plan_strength_cycle", INPUT, ctx=ctx)
        plan = await db.get(Plan, uuid.UUID(out["plan_id"]))

        assert plan is not None
        assert plan.type == "strength"
        assert plan.status == "active"
        assert len(plan.payload["weeks"]) == 8
        assert set(plan.payload["weeks"][0]["lifts"]) == {"深蹲", "卧推"}

    @pytest.mark.asyncio
    async def test_get_plan_detail_returns_one_week(self, db, seeded_user):
        ctx = ToolContext(user_id=seeded_user, session=db)
        created = await registry.invoke("plan_strength_cycle", INPUT, ctx=ctx)
        detail = await registry.invoke("get_plan_detail", {
            "plan_id": created["plan_id"], "week": 3,
        }, ctx=ctx)

        assert detail["found"] is True
        assert detail["week"] == 3
        assert "深蹲" in detail["lifts"]
        assert "weeks" not in detail

    @pytest.mark.asyncio
    async def test_missing_week_returns_actionable_note(self, db, seeded_user):
        ctx = ToolContext(user_id=seeded_user, session=db)
        created = await registry.invoke("plan_strength_cycle", {
            **INPUT, "weeks": 4,
        }, ctx=ctx)
        detail = await registry.invoke("get_plan_detail", {
            "plan_id": created["plan_id"], "week": 5,
        }, ctx=ctx)
        assert detail["found"] is False
        assert "1–4" in detail["note"]

    @pytest.mark.asyncio
    async def test_cannot_read_other_users_plan(self, db, seeded_user):
        from sqlalchemy import text

        from app.core.database import bind_rls_user
        from app.models.user import User

        ctx = ToolContext(user_id=seeded_user, session=db)
        created = await registry.invoke("plan_strength_cycle", INPUT, ctx=ctx)

        other = User(email=f"plan-other-{uuid.uuid4().hex[:8]}@t.com", password_hash="x")
        db.add(other)
        await db.flush()
        bind_rls_user(db, other.id)
        await db.execute(
            text("SELECT set_config('app.user_id', :uid, true)"), {"uid": str(other.id)},
        )
        detail = await registry.invoke("get_plan_detail", {
            "plan_id": created["plan_id"], "week": 1,
        }, ctx=ToolContext(user_id=other.id, session=db))
        assert detail["found"] is False

    @pytest.mark.asyncio
    async def test_invalid_input_is_rejected(self, db, seeded_user):
        from app.core.tools.registry import ToolValidationError

        with pytest.raises((ToolValidationError, ValueError)):
            await registry.invoke("plan_strength_cycle", {
                "lifts": {}, "weeks": 8, "scheme": "linear",
            }, ctx=ToolContext(user_id=seeded_user, session=db))
