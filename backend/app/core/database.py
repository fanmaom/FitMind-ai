"""数据库连接管理。"""

from contextlib import asynccontextmanager
from typing import AsyncGenerator

import uuid

from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Session

from app.core.config import get_settings
from app.core.logger import logger

settings = get_settings()

engine = create_async_engine(
    settings.database_url,
    pool_size=settings.db_pool_size,
    max_overflow=settings.db_max_overflow,
    pool_pre_ping=True,
    echo=settings.db_echo,
)

async_session_maker = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autocommit=False,
    autoflush=False,
)

# worker 需要用具备 BYPASSRLS 的独立数据库角色跨用户领取 jobs。
# Docker 开发环境的数据库 owner 可直接复用；生产环境应设置 WORKER_DATABASE_URL。
worker_engine = create_async_engine(
    settings.worker_database_url or settings.database_url,
    pool_size=max(1, settings.db_pool_size // 2),
    max_overflow=max(1, settings.db_max_overflow // 2),
    pool_pre_ping=True,
    echo=settings.db_echo,
)
worker_session_maker = async_sessionmaker(
    worker_engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autocommit=False,
    autoflush=False,
)


class Base(DeclarativeBase):
    """SQLAlchemy 模型基类。"""


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI 依赖：提供请求级 session。

    客户端断连时 Starlette 抛 CancelledError，此时事务可能已处于 rollback
    状态，直接 commit 会抛 PendingRollbackError。因此先 rollback 再关闭。
    """
    session = async_session_maker()
    try:
        yield session
    except Exception:
        await session.rollback()
        raise
    finally:
        try:
            await session.close()
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"关闭 session 失败：{exc}")


@asynccontextmanager
async def session_scope() -> AsyncGenerator[AsyncSession, None]:
    """非请求上下文（worker、脚本）使用的 session。"""
    session = async_session_maker()
    try:
        yield session
        await session.commit()
    except Exception:
        await session.rollback()
        raise
    finally:
        await session.close()


# ── RLS 上下文 ────────────────────────────────────────────────────────────
#
# set_config(..., true) 是事务级的，COMMIT / ROLLBACK 之后就失效。这带来一个
# 隐蔽的问题：Agent 一轮会连着调多个工具，写入类工具内部会 commit，之后
# 同一个 session 上的所有查询都会被 RLS 过滤成零行——查不到数据，还不报错。
#
# 不靠"记得在每次 commit 后重设"（那又回到了靠自觉），而是挂事件：
# 任何新事务一开始就自动注入上下文，让它成为 session 的不变量。

RLS_USER_KEY = "rls_user_id"


def bind_rls_user(session: AsyncSession, user_id: uuid.UUID) -> None:
    """把用户身份绑定到 session。此后该 session 的每个事务都会自动带上它。"""
    session.info[RLS_USER_KEY] = str(user_id)


@event.listens_for(Session, "after_begin")
def _apply_rls_context(session: Session, transaction, connection) -> None:
    user_id = session.info.get(RLS_USER_KEY)
    if user_id:
        connection.execute(
            text("SELECT set_config('app.user_id', :uid, true)"), {"uid": user_id},
        )
