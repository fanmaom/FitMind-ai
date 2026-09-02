"""任务 worker 入口：``python -m app.core.jobs.worker``。"""

import asyncio

from app.core.actions.extractor import handle_extract_actions
from app.core.database import async_session_maker, bind_rls_user, worker_session_maker
from app.core.jobs.queue import claim_jobs, complete, fail
from app.core.llm.judge_cache import log_stats as log_judge_stats
from app.core.logger import logger
from app.core.memory.extractor import handle_extract_memory
from app.core.memory.summary import handle_summarize_conversation
from app.models.job import Job

POLL_INTERVAL_S = 2.0
# 每这么多个空闲轮打一次缓存统计。2 秒一轮，150 轮约 5 分钟——
# 够用来观察趋势，又不会把日志刷满。
_STATS_EVERY_IDLE_ROUNDS = 150
HANDLERS = {
    "extract_memory": handle_extract_memory,
    "extract_actions": handle_extract_actions,
    "summarize_conversation": handle_summarize_conversation,
}


async def _run_one(job: Job) -> None:
    """在独立事务中执行任务，并显式绑定该任务用户的 RLS。"""
    handler = HANDLERS.get(job.type)
    if handler is None:
        raise ValueError(f"未知任务类型：{job.type}")
    session = async_session_maker()
    bind_rls_user(session, job.user_id)
    try:
        await handler(session, job.payload)
        await session.commit()
    except Exception:
        await session.rollback()
        raise
    finally:
        await session.close()


async def _record_result(job: Job, error: Exception | None) -> None:
    """在 worker 连接中重新加载任务，避免跨 session 使用 detached ORM 对象。"""
    session = worker_session_maker()
    try:
        current = await session.get(Job, job.id)
        if current is None:
            raise RuntimeError(f"任务在执行后消失：{job.id}")
        if error is None:
            await complete(session, current)
        else:
            await fail(session, current, str(error))
        await session.commit()
    except Exception:
        await session.rollback()
        raise
    finally:
        await session.close()


async def run_once() -> int:
    """领取并执行一批任务；返回领取数量，便于测试和进程循环复用。"""
    claim_session = worker_session_maker()
    try:
        jobs = await claim_jobs(claim_session, limit=10)
        await claim_session.commit()
    finally:
        await claim_session.close()

    for job in jobs:
        error: Exception | None = None
        try:
            await _run_one(job)
            logger.info(f"任务完成 {job.type} {job.id}")
        except Exception as exc:  # noqa: BLE001
            error = exc
            logger.exception(f"任务失败 {job.type} {job.id}")
        await _record_result(job, error)
    return len(jobs)


async def run_forever() -> None:
    logger.info(f"worker 启动，轮询间隔 {POLL_INTERVAL_S}s，处理类型：{sorted(HANDLERS)}")
    idle_rounds = 0
    while True:
        if await run_once() == 0:
            idle_rounds += 1
            # 空闲时周期性打一次判定缓存命中率。判定调用实测数秒一次，
            # 命中率是判断"这层缓存有没有用"的唯一依据——没有它就只能猜。
            if idle_rounds % _STATS_EVERY_IDLE_ROUNDS == 0:
                log_judge_stats()
            await asyncio.sleep(POLL_INTERVAL_S)
        else:
            idle_rounds = 0


if __name__ == "__main__":
    asyncio.run(run_forever())
