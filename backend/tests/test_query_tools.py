"""查询类工具：返回聚合值而非原始行。"""

from datetime import date, timedelta

import pytest

from app.core.tools.registry import ToolContext, load_tools, registry
from app.models.body_metric import BodyMetric
from app.models.workout_log import WorkoutLog


@pytest.fixture(scope="module", autouse=True)
def _load():
    load_tools()


class TestQueryWorkoutHistory:
    async def _seed(self, db, user_id, weights: list[float], exercise="深蹲"):
        today = date.today()
        for i, w in enumerate(weights):
            db.add(WorkoutLog(
                user_id=user_id, date=today - timedelta(weeks=len(weights) - 1 - i),
                exercise=exercise, sets=[{"weight": w, "reps": 5}] * 3,
                idempotency_key=f"{exercise}-{i}-{w}",
            ))
        await db.flush()

    @pytest.mark.asyncio
    async def test_returns_aggregate_not_raw_rows(self, db, seeded_user):
        """必须返回聚合值——几百条原始行塞进上下文既爆 Token，
        又逼模型自己做算术，而模型做算术会错。"""
        await self._seed(db, seeded_user, [100, 105, 110, 115, 120])

        out = await registry.invoke("query_workout_history", {
            "exercise": "深蹲", "weeks": 8,
        }, ctx=ToolContext(user_id=seeded_user, session=db))

        assert "sets" not in out, "把原始组数据吐回上下文了"
        assert out["session_count"] == 5
        assert out["start_weight_kg"] == 100
        assert out["latest_weight_kg"] == 120
        assert out["volume_change_pct"] == pytest.approx(20.0)
        assert out["best_weight_kg"] == 120

    @pytest.mark.asyncio
    async def test_best_weight_is_max_not_latest(self, db, seeded_user):
        """最好成绩和最近成绩是两回事——退步时不能报最近的当最好。"""
        await self._seed(db, seeded_user, [100, 130, 110], exercise="卧推")
        out = await registry.invoke("query_workout_history", {
            "exercise": "卧推", "weeks": 8,
        }, ctx=ToolContext(user_id=seeded_user, session=db))
        assert out["best_weight_kg"] == 130
        assert out["latest_weight_kg"] == 110

    @pytest.mark.asyncio
    async def test_empty_is_explicit_not_silent(self, db, seeded_user):
        """查无数据要说清楚，让模型换个说法回复，而不是重试——
        重试一百次也还是查无数据。"""
        out = await registry.invoke("query_workout_history", {
            "exercise": "引体向上", "weeks": 8,
        }, ctx=ToolContext(user_id=seeded_user, session=db))
        assert out["session_count"] == 0
        assert "没有" in out["note"]

    @pytest.mark.asyncio
    async def test_respects_time_window(self, db, seeded_user):
        """窗口外的记录不能算进来。"""
        today = date.today()
        db.add(WorkoutLog(user_id=seeded_user, date=today - timedelta(weeks=30),
                          exercise="划船", sets=[{"weight": 60, "reps": 10}],
                          idempotency_key="old-row"))
        await db.flush()
        out = await registry.invoke("query_workout_history", {
            "exercise": "划船", "weeks": 4,
        }, ctx=ToolContext(user_id=seeded_user, session=db))
        assert out["session_count"] == 0


class TestQueryBodyTrend:
    @pytest.mark.asyncio
    async def test_returns_rate(self, db, seeded_user):
        today = date.today()
        for i, w in enumerate([84.0, 83.2, 82.5, 81.6]):
            db.add(BodyMetric(user_id=seeded_user, date=today - timedelta(weeks=3 - i),
                              weight_kg=w, idempotency_key=f"bm-{i}"))
        await db.flush()

        out = await registry.invoke("query_body_trend", {
            "weeks": 8,
        }, ctx=ToolContext(user_id=seeded_user, session=db))

        assert out["record_count"] == 4
        assert out["start_kg"] == 84.0
        assert out["latest_kg"] == 81.6
        assert out["total_change_kg"] == pytest.approx(-2.4)
        assert out["weekly_rate_kg"] < 0

    @pytest.mark.asyncio
    async def test_single_record_cannot_compute_trend(self, db, seeded_user):
        db.add(BodyMetric(user_id=seeded_user, date=date.today(),
                          weight_kg=80.0, idempotency_key="solo"))
        await db.flush()
        out = await registry.invoke("query_body_trend", {"weeks": 8},
                                    ctx=ToolContext(user_id=seeded_user, session=db))
        assert out["record_count"] == 1
        assert "不足" in out["note"]

    @pytest.mark.asyncio
    async def test_same_day_records_do_not_divide_by_zero(self, db, seeded_user):
        """同一天两条记录时时间跨度为 0，除法会炸。"""
        today = date.today()
        for i, w in enumerate([80.0, 79.5]):
            db.add(BodyMetric(user_id=seeded_user, date=today,
                              weight_kg=w, idempotency_key=f"same-day-{i}"))
        await db.flush()
        out = await registry.invoke("query_body_trend", {"weeks": 8},
                                    ctx=ToolContext(user_id=seeded_user, session=db))
        assert out["record_count"] == 2
        assert isinstance(out["weekly_rate_kg"], float)


class TestQueryToolsAreReadonly:
    @pytest.mark.parametrize("name", ["query_workout_history", "query_body_trend"])
    def test_marked_readonly(self, name):
        assert registry.get(name).readonly is True
