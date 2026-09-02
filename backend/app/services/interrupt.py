"""中断登记表。

用户点「停止」时前端打一个 interrupt 请求，这里记下旗子；Agent 循环在推每个
事件之前查一次，见旗即收尾。

**键为什么是 conversation_id 而不是 message id**：前端在收到 message_start
之前就可能点停止（手快，或首个 token 迟迟不来），那时它手上只有
conversation_id。用 message id 会漏掉最该中断的那一段等待。

## 为什么是「内存 + 广播」而不是查库

`is_requested` 的调用频率是**每个 token 一次**。把它做成数据库查询意味着每个
token 一条 SELECT——这是热路径上最不该出现的东西。所以旗子始终存在进程内存里，
查询永远是一次 set 命中。

跨进程的问题用 PostgreSQL 的 LISTEN/NOTIFY 解决：`request()` 除了写本地 set，
还广播一条通知；每个进程有一个后台监听任务，收到通知就写进**自己的** set。
这样多 uvicorn worker 或多副本部署时，interrupt 请求落到哪个进程都能生效，
而热路径的成本一点没变。

不引入 Redis 是因为数据库已经在那里了，LISTEN/NOTIFY 不需要任何新依赖。

## 降级行为

监听器起不来（连接失败）时只告警，不阻止启动：那样退化成原来的单进程行为
——本进程内的中断照常工作，跨进程失效。这比因为一个辅助通道连不上就整个
服务起不来要好。
"""

import asyncio
import contextlib
import uuid

import asyncpg

from app.core.config import get_settings
from app.core.logger import logger

CHANNEL = "fitmind_interrupt"

# set 而非 dict：request 天然幂等，用户连点两下停止只留一面旗子。
_requested: set[uuid.UUID] = set()

# 广播用的两个句柄。监听连接要长期保持，发布则每次现连——发布频率极低
# （用户点停止才有），不值得为它维护一个常驻连接和重连逻辑。
_listener_conn: asyncpg.Connection | None = None
# 监听连接所属的事件循环。asyncpg 连接绑定在创建它的循环上，换了循环再碰
# 会抛 "future belongs to a different loop"。记下它，循环变了就重建而不是
# 复用——uvicorn --reload 和测试都会换循环。
_listener_loop: asyncio.AbstractEventLoop | None = None
_listener_task: asyncio.Task | None = None

# 已清理的会话也要广播，否则另一个进程的旗子会一直留着，把该会话的**下一个**
# 回合一启动就杀掉。前缀区分两种消息。
_REQUEST_PREFIX = "req:"
_CLEAR_PREFIX = "clr:"


def _dsn() -> str:
    """asyncpg 直连用的 DSN。SQLAlchemy 的 URL 带 +asyncpg 后缀，asyncpg 不认。"""
    return get_settings().database_url.replace("postgresql+asyncpg://", "postgresql://")


def request(conversation_id: uuid.UUID) -> None:
    """标记该会话待中断。重复调用等价于调用一次。

    只写本地 set，不广播。跨进程广播由 request_and_broadcast 负责——那是个
    协程，能在返回前确认广播已发出。同步版本留给不在异步上下文里的调用方。
    """
    _requested.add(conversation_id)


async def request_and_broadcast(conversation_id: uuid.UUID) -> None:
    """标记待中断，并等广播真正发出去再返回。

    HTTP 端点用这个而不是 request()：用户点了停止，请求返回 204 的时候广播
    应该已经在路上了。用 fire-and-forget 的话，进程恰好在这之后关闭（部署、
    重启）会把通知丢掉，而用户已经看到"已停止"了。
    """
    _requested.add(conversation_id)
    await _publish(f"{_REQUEST_PREFIX}{conversation_id}")


def is_requested(conversation_id: uuid.UUID) -> bool:
    """热路径：每个 token 调一次，必须是纯内存查询。"""
    return conversation_id in _requested


def clear(conversation_id: uuid.UUID) -> None:
    """清掉旗子（同步版本，不广播）。

    留给不在异步上下文里的调用方。异步语境请用 clear_and_broadcast——
    fire-and-forget 的广播在进程/循环即将结束时来不及发出，反而留下悬空任务。
    """
    _requested.discard(conversation_id)


async def clear_and_broadcast(conversation_id: uuid.UUID) -> None:
    """清掉旗子并广播。没有旗子时不广播。

    必须广播清理，否则另一个进程的旗子会一直留着，把该会话的**下一个**回合
    一启动就杀掉——正是那种「上次点了停止，之后第一条消息永远没反应」的现象，
    只是换成了跨进程版本。
    """
    had = conversation_id in _requested
    _requested.discard(conversation_id)
    # 只在确实清掉了东西时广播，避免 run_turn 每次收尾都发一条无用通知。
    if had:
        await _publish(f"{_CLEAR_PREFIX}{conversation_id}")


async def _publish(payload: str) -> None:
    """广播给其他进程。失败只告警——本进程的旗子已经生效了。"""
    try:
        conn = await asyncpg.connect(_dsn(), timeout=2.0)
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"中断广播连接失败，仅本进程生效：{exc}")
        return
    try:
        await conn.execute("SELECT pg_notify($1, $2)", CHANNEL, payload)
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"中断广播失败，仅本进程生效：{exc}")
    finally:
        with contextlib.suppress(Exception):
            await conn.close()


def _on_notify(_conn, _pid, _channel, payload: str) -> None:
    """收到其他进程的广播，同步到本地 set。"""
    try:
        if payload.startswith(_REQUEST_PREFIX):
            _requested.add(uuid.UUID(payload[len(_REQUEST_PREFIX):]))
        elif payload.startswith(_CLEAR_PREFIX):
            _requested.discard(uuid.UUID(payload[len(_CLEAR_PREFIX):]))
    except ValueError:
        logger.warning(f"忽略无法解析的中断广播：{payload[:80]}")


async def start_listener() -> bool:
    """启动跨进程监听。返回是否成功。

    失败只告警不抛：退化成单进程行为（本进程内的中断照常工作），比因为一个
    辅助通道连不上就让整个服务起不来要好。
    """
    global _listener_conn, _listener_loop

    loop = asyncio.get_running_loop()
    if _listener_conn is not None:
        if _listener_loop is loop:
            return True
        # 换了事件循环。旧连接绑在死掉的循环上，碰它会抛
        # "future belongs to a different loop"，只能丢掉重建。
        logger.info("事件循环已更换，重建中断广播监听连接")
        _listener_conn = None
        _listener_loop = None

    try:
        conn = await asyncpg.connect(_dsn(), timeout=5.0)
        await conn.add_listener(CHANNEL, _on_notify)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            f"中断广播监听启动失败，多进程部署时跨进程中断将失效：{exc}",
        )
        return False
    _listener_conn = conn
    _listener_loop = loop
    logger.info(f"中断广播监听已启动，频道 {CHANNEL}")
    return True


async def stop_listener() -> None:
    global _listener_conn, _listener_loop

    if _listener_conn is None:
        return
    conn, _listener_conn = _listener_conn, None
    owner_loop, _listener_loop = _listener_loop, None

    # 只有在连接所属的循环里才能安全关它。循环已经换掉时直接丢弃——那条
    # 连接会随旧循环一起消失，硬关只会抛异常。
    if owner_loop is asyncio.get_running_loop():
        with contextlib.suppress(Exception):
            await conn.remove_listener(CHANNEL, _on_notify)
        with contextlib.suppress(Exception):
            await conn.close()
