"""任务 worker 入口：``python -m app.core.jobs.worker``。"""

import asyncio
import contextlib
import signal

from app.core.actions.extractor import handle_extract_actions
from app.core.database import async_session_maker, bind_rls_user, worker_session_maker
from app.core.jobs.queue import claim_jobs, complete, fail, reclaim_stale
from app.core.llm.judge_cache import log_stats as log_judge_stats
from app.core.logger import logger
from app.core.memory.extractor import handle_extract_memory
from app.core.memory.summary import handle_summarize_conversation
from app.models.job import Job

POLL_INTERVAL_S = 2.0
# 每这么多个空闲轮打一次缓存统计。2 秒一轮，150 轮约 5 分钟——
# 够用来观察趋势，又不会把日志刷满。
_STATS_EVERY_IDLE_ROUNDS = 150
# 每这么多个空闲轮跑一次看门狗。回收只在 worker 异常退出后才有事可做，
# 频繁扫描没有意义；30 轮约 1 分钟。
_RECLAIM_EVERY_IDLE_ROUNDS = 30
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


async def run_reclaim() -> int:
    """跑一轮看门狗，回收卡死的 running 任务。"""
    session = worker_session_maker()
    try:
        count = await reclaim_stale(session)
        await session.commit()
        return count
    except Exception as exc:  # noqa: BLE001
        await session.rollback()
        # 回收失败不能把 worker 打死——它是个后台清理动作，正常的任务处理
        # 比它重要。下一轮再试。
        logger.warning(f"回收卡死任务失败，下一轮重试：{exc}")
        return 0
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
        except asyncio.CancelledError:
            # 收到停机信号。把这条标成失败让它退避重试，而不是留在 running
            # 等看门狗十分钟后才回收——优雅退出的意义就在于此。
            logger.warning(f"任务被中断，标记为可重试：{job.type} {job.id}")
            await _record_result(job, RuntimeError("worker 停机，任务未完成"))
            raise
        except Exception as exc:  # noqa: BLE001
            error = exc
            logger.exception(f"任务失败 {job.type} {job.id}")
        await _record_result(job, error)
    return len(jobs)


def _install_signal_handlers(stopping: asyncio.Event) -> None:
    """把 SIGTERM / SIGINT 转成一个事件，让主循环自己决定何时收手。

    容器不响应 SIGTERM 的后果是 docker stop 等满宽限期后发 SIGKILL（退出码
    137），执行中的任务被硬切、永久停在 running。装上处理器之后，停机变成
    "跑完手上这批就退"，正在执行的任务能被正常标记。

    add_signal_handler 在部分平台（Windows）不支持，捕获后退化成默认行为，
    不因此启动失败。
    """
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(NotImplementedError, AttributeError):
            loop.add_signal_handler(sig, stopping.set)


async def run_forever(stopping: asyncio.Event | None = None) -> None:
    logger.info(f"worker 启动，轮询间隔 {POLL_INTERVAL_S}s，处理类型：{sorted(HANDLERS)}")
    if stopping is None:
        stopping = asyncio.Event()
        _install_signal_handlers(stopping)

    # 启动时先回收一次：上一个进程如果是被 SIGKILL 打死的，它领走的任务
    # 正卡在 running。不在这里捞，就要等看门狗的空闲轮到来。
    reclaimed = await run_reclaim()
    if reclaimed:
        logger.info(f"启动时回收了 {reclaimed} 条卡死任务")

    idle_rounds = 0
    while not stopping.is_set():
        if await run_once() == 0:
            idle_rounds += 1
            if idle_rounds % _RECLAIM_EVERY_IDLE_ROUNDS == 0:
                await run_reclaim()
            # 空闲时周期性打一次判定缓存命中率。判定调用实测数秒一次，
            # 命中率是判断"这层缓存有没有用"的唯一依据——没有它就只能猜。
            if idle_rounds % _STATS_EVERY_IDLE_ROUNDS == 0:
                log_judge_stats()
            # 等待而不是硬睡：收到停机信号时立刻醒，不用把 2 秒睡满。
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(stopping.wait(), timeout=POLL_INTERVAL_S)
        else:
            idle_rounds = 0

    logger.info("worker 收到停机信号，已退出轮询")


if __name__ == "__main__":
    asyncio.run(run_forever())
