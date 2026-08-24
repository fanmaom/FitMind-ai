"""数据库连接管理。"""

from contextlib import asynccontextmanager
from typing import AsyncGenerator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

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
