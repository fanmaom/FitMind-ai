"""测试环境准备。

测试不依赖真实 .env——在导入任何应用模块之前就把环境变量设好，
这样 CI 里没有 .env 文件也能跑。
"""

import os

import pytest_asyncio

os.environ.setdefault(
    "DATABASE_URL", "postgresql+asyncpg://fitness:fitness@localhost:5432/fitness",
)
os.environ.setdefault("JWT_SECRET", "t" * 32)
os.environ.setdefault("LLM_API_KEY", "sk-test-not-a-real-key")
os.environ.setdefault("LLM_MODEL", "test-model")


@pytest_asyncio.fixture(autouse=True)
async def _reset_engine_pool():
    """每个测试结束后清空连接池。

    database.py 的 engine 是模块级单例带连接池，而 pytest-asyncio 给每个测试
    一个新事件循环。asyncpg 连接绑定在创建它的循环上，跨测试复用会报
    "Event loop is closed"。

    注意不能改用 NullPool 来规避：T4 的并发隔离测试要靠连接复用才能暴露
    SET（会话级）泄漏。连接池在单个测试内部照常工作，只是测试之间不复用，
    这对那个测试毫无影响。
    """
    yield
    from app.core.database import engine

    await engine.dispose()


@pytest_asyncio.fixture
async def db():
    """请求级 session。测试结束回滚，不留脏数据。"""
    from app.core.database import async_session_maker

    session = async_session_maker()
    try:
        yield session
    finally:
        await session.rollback()
        await session.close()


@pytest_asyncio.fixture
async def seeded_user(db):
    """建一个用户并把 RLS 上下文切到他名下。"""
    import uuid as _uuid

    from sqlalchemy import text

    from app.models.user import User

    from app.core.database import bind_rls_user

    user = User(email=f"tool-{_uuid.uuid4().hex[:8]}@test.com", password_hash="x")
    db.add(user)
    await db.flush()
    bind_rls_user(db, user.id)
    await db.execute(text("SELECT set_config('app.user_id', :uid, true)"), {"uid": str(user.id)})
    return user.id
