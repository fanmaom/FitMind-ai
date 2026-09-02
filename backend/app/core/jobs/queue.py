"""PostgreSQL 任务队列操作。"""

import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logger import logger
from app.models.job import Job

MAX_ATTEMPTS = 3
BACKOFF_SECONDS = (30, 120, 600)

# 一条任务在 running 状态最多待多久。超过就认为执行它的 worker 已经死了。
#
# 必须显著大于任务的正常耗时，否则会把还在跑的任务判成卡死、造成重复执行。
# 当前最慢的是待办抽取：一次抽取调用 + 最多 3 条待办 × 3 候选判定，单次判定
# 实测 2.8–4.3 秒，最坏情况几十秒。取 10 分钟留足余量。
STALE_RUNNING_AFTER_S = 600


async def enqueue(
    session: AsyncSession,
    job_type: str,
    payload: dict,
    user_id: uuid.UUID | None = None,
) -> Job:
    if user_id is None:
        raw = (
            await session.execute(
                text("SELECT NULLIF(current_setting('app.user_id', true), '')"),
            )
        ).scalar_one_or_none()
        user_id = uuid.UUID(raw) if raw else None
    if user_id is None:
        raise ValueError("enqueue 需要 user_id：任务表同样受 RLS 约束")

    job = Job(user_id=user_id, type=job_type, payload=payload)
    session.add(job)
    await session.flush()
    return job


async def claim_jobs(session: AsyncSession, limit: int = 10) -> list[Job]:
    """锁定并领取可运行任务；并发 worker 会立即跳过已锁定行。"""
    if limit <= 0:
        return []
    now = datetime.now(timezone.utc)
    jobs = (
        await session.scalars(
            select(Job)
            .where(Job.status == "pending", Job.run_after <= now)
            .order_by(Job.created_at)
            .limit(limit)
            .with_for_update(skip_locked=True),
        )
    ).all()
    for job in jobs:
        job.status = "running"
        # 记下领取时刻，看门狗靠它判断这条 running 是否卡死。
        job.claimed_at = now
    await session.flush()
    return list(jobs)


async def reclaim_stale(
    session: AsyncSession, stale_after_s: int = STALE_RUNNING_AFTER_S,
) -> int:
    """把卡死的 running 任务放回 pending，返回回收条数。

    worker 在执行途中硬退出（SIGKILL、OOM、容器被杀）时，那条任务永久停在
    running。claim_jobs 只捞 pending，所以没有任何机制会再碰它——事实抽取或
    待办抽取就此丢失，而且不报错、不重试，只是那一条永远不出现。

    回收时**照常累加 attempts**，让它继续受 MAX_ATTEMPTS 约束。不加的话，
    一个必然会把 worker 打死的任务（比如某段输入触发了 OOM）会被无限回收、
    无限打死 worker，把整个队列堵住。累加之后它最多再试两次就进 failed，
    留下 last_error 供排查。

    attempts 已经到上限的直接转 failed，不再放回——放回去也只会被
    claim → 立刻判定超限，多绕一圈。
    """
    threshold = datetime.now(timezone.utc) - timedelta(seconds=stale_after_s)

    stale = (
        await session.scalars(
            select(Job)
            .where(
                Job.status == "running",
                Job.claimed_at.is_not(None),
                Job.claimed_at < threshold,
            )
            .with_for_update(skip_locked=True),
        )
    ).all()
    if not stale:
        return 0

    now = datetime.now(timezone.utc)
    for job in stale:
        job.attempts += 1
        job.claimed_at = None
        job.last_error = (
            f"执行超过 {stale_after_s}s 未收尾，判定为 worker 异常退出后回收"
        )
        if job.attempts >= MAX_ATTEMPTS:
            job.status = "failed"
            logger.error(
                f"任务回收后已达重试上限，转 failed：{job.type} {job.id}",
            )
        else:
            job.status = "pending"
            delay = BACKOFF_SECONDS[min(job.attempts - 1, len(BACKOFF_SECONDS) - 1)]
            job.run_after = now + timedelta(seconds=delay)
            logger.warning(
                f"回收卡死任务：{job.type} {job.id}"
                f"（第 {job.attempts} 次，{delay}s 后重试）",
            )
    await session.flush()
    return len(stale)


async def complete(session: AsyncSession, job: Job) -> None:
    job.status = "done"
    # 清掉领取时刻：留着会让排查时误以为它还在跑，也让看门狗的索引里
    # 堆积无意义的行。
    job.claimed_at = None
    await session.flush()


async def fail(session: AsyncSession, job: Job, error: str) -> None:
    """记录失败并退避重试；达到上限后保留错误并转为 failed。"""
    job.attempts += 1
    job.last_error = error[:2000]
    job.claimed_at = None
    if job.attempts >= MAX_ATTEMPTS:
        job.status = "failed"
    else:
        job.status = "pending"
        delay = BACKOFF_SECONDS[min(job.attempts - 1, len(BACKOFF_SECONDS) - 1)]
        job.run_after = datetime.now(timezone.utc) + timedelta(seconds=delay)
    await session.flush()
