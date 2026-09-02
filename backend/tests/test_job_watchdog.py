"""看门狗：回收卡死的 running 任务，与优雅退出。

worker 在执行途中硬退出（SIGKILL、OOM、容器被杀）时，那条任务永久停在
running。claim_jobs 只捞 pending，所以没有任何机制会再碰它——事实抽取或待办
抽取就此丢失，而且不报错、不重试，只是那一条永远不出现。
"""

import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.core.jobs.queue import (
    MAX_ATTEMPTS,
    STALE_RUNNING_AFTER_S,
    claim_jobs,
    complete,
    enqueue,
    fail,
    reclaim_stale,
)
from app.models.job import Job


async def _running_job(db, user_id, *, age_s: int, attempts: int = 0) -> Job:
    job = await enqueue(db, "extract_memory", {"x": 1}, user_id=user_id)
    job.status = "running"
    job.attempts = attempts
    job.claimed_at = datetime.now(timezone.utc) - timedelta(seconds=age_s)
    await db.flush()
    return job


class TestClaimRecordsTime:
    @pytest.mark.asyncio
    async def test_claim_sets_claimed_at(self, db, seeded_user):
        """没有领取时刻就没法判断 running 卡了多久。created_at 不行——
        那是入队时间，排队很久才被领走的任务会被误判成超时。"""
        await enqueue(db, "extract_memory", {}, user_id=seeded_user)
        await db.flush()

        jobs = await claim_jobs(db, limit=5)
        assert jobs
        assert all(j.claimed_at is not None for j in jobs)

    @pytest.mark.asyncio
    async def test_complete_clears_claimed_at(self, db, seeded_user):
        """终态留着领取时刻会让排查时误以为它还在跑。"""
        await enqueue(db, "extract_memory", {}, user_id=seeded_user)
        await db.flush()
        job = (await claim_jobs(db, limit=1))[0]
        await complete(db, job)
        assert job.claimed_at is None

    @pytest.mark.asyncio
    async def test_fail_clears_claimed_at(self, db, seeded_user):
        await enqueue(db, "extract_memory", {}, user_id=seeded_user)
        await db.flush()
        job = (await claim_jobs(db, limit=1))[0]
        await fail(db, job, "boom")
        assert job.claimed_at is None


class TestReclaim:
    @pytest.mark.asyncio
    async def test_stale_running_goes_back_to_pending(self, db, seeded_user):
        job = await _running_job(db, seeded_user, age_s=STALE_RUNNING_AFTER_S + 60)

        assert await reclaim_stale(db) == 1
        await db.refresh(job)
        assert job.status == "pending"
        assert job.claimed_at is None
        assert "worker 异常退出" in job.last_error

    @pytest.mark.asyncio
    async def test_fresh_running_is_left_alone(self, db, seeded_user):
        """还在跑的任务不能被回收——那会造成重复执行。"""
        job = await _running_job(db, seeded_user, age_s=5)

        assert await reclaim_stale(db) == 0
        await db.refresh(job)
        assert job.status == "running"

    @pytest.mark.asyncio
    async def test_pending_jobs_untouched(self, db, seeded_user):
        job = await enqueue(db, "extract_memory", {}, user_id=seeded_user)
        await db.flush()

        assert await reclaim_stale(db) == 0
        await db.refresh(job)
        assert job.status == "pending"
        assert job.attempts == 0

    @pytest.mark.asyncio
    async def test_reclaim_counts_as_an_attempt(self, db, seeded_user):
        """必须累加 attempts。不加的话，一个必然会把 worker 打死的任务
        （某段输入触发 OOM）会被无限回收、无限打死 worker，堵住整个队列。"""
        job = await _running_job(db, seeded_user, age_s=STALE_RUNNING_AFTER_S + 1)

        await reclaim_stale(db)
        await db.refresh(job)
        assert job.attempts == 1

    @pytest.mark.asyncio
    async def test_exhausted_attempts_go_to_failed(self, db, seeded_user):
        """attempts 到上限的直接转 failed，不再放回——放回去也只会被
        claim 后立刻判定超限，多绕一圈。"""
        job = await _running_job(
            db, seeded_user,
            age_s=STALE_RUNNING_AFTER_S + 1, attempts=MAX_ATTEMPTS - 1,
        )

        await reclaim_stale(db)
        await db.refresh(job)
        assert job.status == "failed"
        assert job.attempts == MAX_ATTEMPTS

    @pytest.mark.asyncio
    async def test_reclaimed_job_has_backoff(self, db, seeded_user):
        """立刻可领会让一个反复超时的任务瞬间刷满重试次数。"""
        job = await _running_job(db, seeded_user, age_s=STALE_RUNNING_AFTER_S + 1)
        await reclaim_stale(db)
        await db.refresh(job)
        assert job.run_after > datetime.now(timezone.utc)

    @pytest.mark.asyncio
    async def test_reclaimed_job_is_claimable_after_backoff(self, db, seeded_user):
        """回收的意义在于它能被重新领走。只改状态不够。"""
        job = await _running_job(db, seeded_user, age_s=STALE_RUNNING_AFTER_S + 1)
        await reclaim_stale(db)
        await db.refresh(job)

        job.run_after = datetime.now(timezone.utc) - timedelta(seconds=1)
        await db.flush()
        assert job.id in [j.id for j in await claim_jobs(db, limit=10)]

    @pytest.mark.asyncio
    async def test_null_claimed_at_is_skipped(self, db, seeded_user):
        """claimed_at 为空的 running 无从判断卡了多久，宁可放过——
        误回收会造成重复执行。"""
        job = await enqueue(db, "extract_memory", {}, user_id=seeded_user)
        job.status = "running"
        job.claimed_at = None
        await db.flush()

        assert await reclaim_stale(db) == 0

    @pytest.mark.asyncio
    async def test_custom_threshold(self, db, seeded_user):
        job = await _running_job(db, seeded_user, age_s=30)
        assert await reclaim_stale(db, stale_after_s=3600) == 0
        assert await reclaim_stale(db, stale_after_s=10) == 1
        await db.refresh(job)
        assert job.status == "pending"

    @pytest.mark.asyncio
    async def test_multiple_stale_jobs_all_reclaimed(self, db, seeded_user):
        for _ in range(3):
            await _running_job(db, seeded_user, age_s=STALE_RUNNING_AFTER_S + 1)

        assert await reclaim_stale(db) == 3
        remaining = (await db.scalars(
            select(Job).where(Job.status == "running"),
        )).all()
        assert remaining == []


class TestThresholdSanity:
    def test_threshold_exceeds_slowest_task(self):
        """阈值必须显著大于任务正常耗时，否则会把还在跑的判成卡死。
        最慢的是待办抽取：一次抽取调用 + 最多 3 条 × 3 候选判定，
        单次判定实测 2.8–4.3 秒。"""
        worst_case_s = 1 * 5 + 3 * 3 * 5
        assert STALE_RUNNING_AFTER_S > worst_case_s * 2


class TestGracefulShutdown:
    @pytest.mark.asyncio
    async def test_loop_exits_when_event_set(self):
        """收到停机信号要退出轮询，而不是等被 SIGKILL。"""
        from app.core.jobs import worker

        stopping = asyncio.Event()
        stopping.set()
        await asyncio.wait_for(worker.run_forever(stopping), timeout=5)

    @pytest.mark.asyncio
    async def test_loop_wakes_immediately_on_signal(self, monkeypatch):
        """空闲等待要能被信号打断，否则每次停机都要白等一个轮询周期。"""
        from app.core.jobs import worker

        monkeypatch.setattr(worker, "POLL_INTERVAL_S", 30.0)

        async def no_jobs() -> int:
            return 0

        async def no_reclaim() -> int:
            return 0

        monkeypatch.setattr(worker, "run_once", no_jobs)
        monkeypatch.setattr(worker, "run_reclaim", no_reclaim)

        stopping = asyncio.Event()

        async def stop_soon():
            await asyncio.sleep(0.05)
            stopping.set()

        # 若等待不可中断，这里会耗满 30 秒的 POLL_INTERVAL_S 而超时。
        await asyncio.wait_for(
            asyncio.gather(worker.run_forever(stopping), stop_soon()), timeout=5,
        )

    @pytest.mark.asyncio
    async def test_reclaim_runs_at_startup(self, monkeypatch):
        """上一个进程被 SIGKILL 打死时它领走的任务正卡在 running。
        不在启动时捞，就要等看门狗的空闲轮到来。"""
        from app.core.jobs import worker

        called = []

        async def spy_reclaim() -> int:
            called.append(1)
            return 0

        monkeypatch.setattr(worker, "run_reclaim", spy_reclaim)

        stopping = asyncio.Event()
        stopping.set()
        await asyncio.wait_for(worker.run_forever(stopping), timeout=5)
        assert called, "启动时没有回收，上个进程留下的孤儿要等一分钟才被处理"

    @pytest.mark.asyncio
    async def test_reclaim_failure_does_not_kill_worker(self, monkeypatch):
        """回收是后台清理动作，比它重要的是正常任务处理。"""
        from app.core.jobs import worker

        async def broken_reclaim(_session, **_kw):
            raise RuntimeError("数据库连接断了")

        monkeypatch.setattr(worker, "reclaim_stale", broken_reclaim)
        assert await worker.run_reclaim() == 0


class TestInterruptedJobIsMarked:
    """停机时正在执行的任务要标成可重试，而不是留在 running 等看门狗十分钟后
    才回收——优雅退出的意义就在于此。

    这里只验证 run_once 的取消分支**调用了** _record_result 并传入了非空错误。
    不去查库断言状态：_record_result 用 worker_session_maker（BYPASSRLS 的
    独立角色）自行提交，与测试的 db session 不共享事务，refresh 看不到它写的
    东西。fail() 本身的退避语义由 test_jobs.py 覆盖。
    """

    @pytest.mark.asyncio
    async def test_cancelled_job_is_recorded_as_failure(
        self, db, seeded_user, monkeypatch,
    ):
        from app.core.jobs import worker

        job = await enqueue(db, "extract_memory", {}, user_id=seeded_user)
        await db.flush()

        recorded: list[tuple] = []

        async def one_job(_session, limit=10):
            return [job]

        async def cancelled_run(_job):
            raise asyncio.CancelledError

        async def spy_record(target, error):
            recorded.append((target.id, error))

        monkeypatch.setattr(worker, "claim_jobs", one_job)
        monkeypatch.setattr(worker, "_run_one", cancelled_run)
        monkeypatch.setattr(worker, "_record_result", spy_record)

        with pytest.raises(asyncio.CancelledError):
            await worker.run_once()

        assert len(recorded) == 1, "被中断的任务没有被记账，会留在 running"
        job_id, error = recorded[0]
        assert job_id == job.id
        assert error is not None and "停机" in str(error)

    @pytest.mark.asyncio
    async def test_cancellation_propagates(self, db, seeded_user, monkeypatch):
        """记账之后必须继续抛，否则停机信号被吞掉、循环照常跑下去。"""
        from app.core.jobs import worker

        job = await enqueue(db, "extract_memory", {}, user_id=seeded_user)
        await db.flush()

        async def one_job(_session, limit=10):
            return [job]

        async def cancelled_run(_job):
            raise asyncio.CancelledError

        async def noop_record(_target, _error):
            return None

        monkeypatch.setattr(worker, "claim_jobs", one_job)
        monkeypatch.setattr(worker, "_run_one", cancelled_run)
        monkeypatch.setattr(worker, "_record_result", noop_record)

        with pytest.raises(asyncio.CancelledError):
            await worker.run_once()

    @pytest.mark.asyncio
    async def test_normal_failure_still_recorded(self, db, seeded_user, monkeypatch):
        """普通异常仍走原来的记账路径，不能被取消分支抢走。"""
        from app.core.jobs import worker

        job = await enqueue(db, "extract_memory", {}, user_id=seeded_user)
        await db.flush()

        recorded: list[tuple] = []

        async def one_job(_session, limit=10):
            return [job]

        async def boom(_job):
            raise RuntimeError("抽取失败")

        async def spy_record(target, error):
            recorded.append((target.id, error))

        monkeypatch.setattr(worker, "claim_jobs", one_job)
        monkeypatch.setattr(worker, "_run_one", boom)
        monkeypatch.setattr(worker, "_record_result", spy_record)

        assert await worker.run_once() == 1
        assert len(recorded) == 1
        assert "抽取失败" in str(recorded[0][1])
