"""记录类工具：写入与幂等。"""

import pytest
from sqlalchemy import select

from app.core.tools.registry import ToolContext, load_tools, registry
from app.models.body_metric import BodyMetric
from app.models.workout_log import WorkoutLog


@pytest.fixture(scope="module", autouse=True)
def _load():
    load_tools()


class TestLogWorkout:
    @pytest.mark.asyncio
    async def test_writes_row_and_returns_summary(self, db, seeded_user):
        ctx = ToolContext(user_id=seeded_user, session=db)
        out = await registry.invoke("log_workout", {
            "date": "2026-08-24", "exercise": "深蹲",
            "sets": [{"weight": 120, "reps": 5}, {"weight": 120, "reps": 5}],
        }, ctx=ctx)

        assert out["deduplicated"] is False
        assert out["set_count"] == 2
        assert out["total_volume_kg"] == pytest.approx(1200.0)
        assert out["top_weight_kg"] == 120

        rows = (await db.scalars(
            select(WorkoutLog).where(WorkoutLog.exercise == "深蹲"),
        )).all()
        assert len(rows) == 1

    @pytest.mark.asyncio
    async def test_is_idempotent(self, db, seeded_user):
        """重试机制遇上写操作，不做幂等就会一次训练记两条，
        后续所有趋势分析全被污染。"""
        ctx = ToolContext(user_id=seeded_user, session=db)
        body = {"date": "2026-08-24", "exercise": "硬拉", "sets": [{"weight": 140, "reps": 3}]}

        first = await registry.invoke("log_workout", body, ctx=ctx)
        second = await registry.invoke("log_workout", body, ctx=ctx)

        assert first["deduplicated"] is False
        assert second["deduplicated"] is True

        rows = (await db.scalars(
            select(WorkoutLog).where(WorkoutLog.exercise == "硬拉"),
        )).all()
        assert len(rows) == 1

    @pytest.mark.asyncio
    async def test_different_content_is_not_deduplicated(self, db, seeded_user):
        """同一天同一动作但重量不同，是两次不同的训练，必须都记下来。"""
        ctx = ToolContext(user_id=seeded_user, session=db)
        await registry.invoke("log_workout", {
            "date": "2026-08-24", "exercise": "划船", "sets": [{"weight": 60, "reps": 10}],
        }, ctx=ctx)
        out = await registry.invoke("log_workout", {
            "date": "2026-08-24", "exercise": "划船", "sets": [{"weight": 70, "reps": 8}],
        }, ctx=ctx)
        assert out["deduplicated"] is False

    @pytest.mark.asyncio
    async def test_bodyweight_exercise_accepted(self, db, seeded_user):
        """自重动作重量为 0，必须允许——否则引体向上记不进来。"""
        ctx = ToolContext(user_id=seeded_user, session=db)
        out = await registry.invoke("log_workout", {
            "date": "2026-08-24", "exercise": "引体向上", "sets": [{"weight": 0, "reps": 8}],
        }, ctx=ctx)
        assert out["deduplicated"] is False
        assert out["top_weight_kg"] == 0


class TestLogBodyMetric:
    @pytest.mark.asyncio
    async def test_writes_row(self, db, seeded_user):
        ctx = ToolContext(user_id=seeded_user, session=db)
        out = await registry.invoke("log_body_metric", {
            "date": "2026-08-24", "weight_kg": 81.6,
        }, ctx=ctx)
        assert out["deduplicated"] is False
        assert out["weight_kg"] == 81.6

        rows = (await db.scalars(select(BodyMetric))).all()
        assert len(rows) == 1

    @pytest.mark.asyncio
    async def test_is_idempotent(self, db, seeded_user):
        ctx = ToolContext(user_id=seeded_user, session=db)
        body = {"date": "2026-08-24", "weight_kg": 80.2}
        await registry.invoke("log_body_metric", body, ctx=ctx)
        second = await registry.invoke("log_body_metric", body, ctx=ctx)
        assert second["deduplicated"] is True


class TestWriteToolsAreNotReadonly:
    """写入类工具必须标 readonly=False，降级层据此决定能否重试。
    误标成只读会导致重试时重复写入。"""

    @pytest.mark.parametrize("name", ["log_workout", "log_body_metric"])
    def test_marked_as_write(self, name):
        assert registry.get(name).readonly is False


class TestRlsContextSurvivesCommit:
    """写入类工具内部会 commit，而 set_config(..., true) 是事务级的。

    若不在每个新事务里重新注入上下文，Agent 一轮连着调多个工具时，
    第一个写入工具 commit 之后，后面所有工具都会读到零行——查不到数据，
    还不报错，是最难排查的一类 bug。
    """

    @pytest.mark.asyncio
    async def test_read_after_write_tool_commit(self, db, seeded_user):
        ctx = ToolContext(user_id=seeded_user, session=db)

        await registry.invoke("log_workout", {
            "date": "2026-08-24", "exercise": "过顶推举", "sets": [{"weight": 50, "reps": 8}],
        }, ctx=ctx)

        # 上面已 commit。若上下文没有自动重建，这里会读到 0 行。
        rows = (await db.scalars(
            select(WorkoutLog).where(WorkoutLog.exercise == "过顶推举"),
        )).all()
        assert len(rows) == 1, "工具 commit 后 RLS 上下文丢失，读不到刚写入的数据"

    @pytest.mark.asyncio
    async def test_multiple_write_tools_in_sequence(self, db, seeded_user):
        """模拟 Agent 一轮连调多个写入工具的真实场景。"""
        ctx = ToolContext(user_id=seeded_user, session=db)

        await registry.invoke("log_workout", {
            "date": "2026-08-24", "exercise": "深蹲", "sets": [{"weight": 120, "reps": 5}],
        }, ctx=ctx)
        await registry.invoke("log_body_metric", {
            "date": "2026-08-24", "weight_kg": 81.6,
        }, ctx=ctx)
        third = await registry.invoke("log_workout", {
            "date": "2026-08-24", "exercise": "卧推", "sets": [{"weight": 90, "reps": 5}],
        }, ctx=ctx)

        assert third["deduplicated"] is False, "第三个工具受前两次 commit 影响"
        assert len((await db.scalars(select(WorkoutLog))).all()) == 2
        assert len((await db.scalars(select(BodyMetric))).all()) == 1
