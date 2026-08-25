"""PostgreSQL 任务队列操作。"""

import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.job import Job

MAX_ATTEMPTS = 3
BACKOFF_SECONDS = (30, 120, 600)


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
    await session.flush()
    return list(jobs)


async def complete(session: AsyncSession, job: Job) -> None:
    job.status = "done"
    await session.flush()


async def fail(session: AsyncSession, job: Job, error: str) -> None:
    """记录失败并退避重试；达到上限后保留错误并转为 failed。"""
    job.attempts += 1
    job.last_error = error[:2000]
    if job.attempts >= MAX_ATTEMPTS:
        job.status = "failed"
    else:
        job.status = "pending"
        delay = BACKOFF_SECONDS[min(job.attempts - 1, len(BACKOFF_SECONDS) - 1)]
        job.run_after = datetime.now(timezone.utc) + timedelta(seconds=delay)
    await session.flush()
