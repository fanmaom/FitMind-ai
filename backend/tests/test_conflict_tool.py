"""目标冲突检测工具。"""

import pytest

from app.core.memory.profile import merge_profile
from app.core.tools.registry import ToolContext, load_tools, registry


@pytest.fixture(scope="module", autouse=True)
def _load():
    load_tools()


@pytest.mark.asyncio
async def test_reads_current_phase_and_returns_clickable_options(db, seeded_user):
    await merge_profile(db, seeded_user, {"goal": "cut", "phase_started_on": "2026-08-01"})
    out = await registry.invoke("check_plan_conflict", {
        "requested_goal": "cut", "wants_strength_gain": True,
    }, ctx=ToolContext(user_id=seeded_user, session=db))
    assert out["has_conflict"] is True
    assert out["current_phase"] == "cut"
    assert len(out["options"]) == 2
    assert all({"key", "title", "summary"} <= set(option) for option in out["options"])
    assert out["__card__"]["type"] == "conflict"


@pytest.mark.asyncio
async def test_non_conflict_has_no_card(db, seeded_user):
    await merge_profile(db, seeded_user, {"goal": "maintain"})
    out = await registry.invoke("check_plan_conflict", {
        "requested_goal": "maintain", "wants_strength_gain": False,
    }, ctx=ToolContext(user_id=seeded_user, session=db))
    assert out["has_conflict"] is False
    assert "__card__" not in out
