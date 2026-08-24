"""数据库连通性与 schema 校验。"""

import pytest
from sqlalchemy import text


@pytest.mark.asyncio
async def test_pgvector_extension_installed(db):
    result = await db.execute(text("SELECT extname FROM pg_extension WHERE extname = 'vector'"))
    assert result.scalar_one() == "vector"


@pytest.mark.asyncio
async def test_users_table_exists(db):
    result = await db.execute(text("SELECT to_regclass('public.users')"))
    assert result.scalar_one() == "users"


@pytest.mark.asyncio
async def test_app_role_is_not_superuser(db):
    """RLS 对 superuser 完全无效——应用必须用普通角色连库，否则隔离策略形同虚设。"""
    result = await db.execute(text("SELECT rolsuper FROM pg_roles WHERE rolname = current_user"))
    assert result.scalar_one() is False, "应用连接用的是 superuser，RLS 会被绕过"


@pytest.mark.asyncio
async def test_can_insert_and_read_user(db):
    import uuid

    from app.models.user import User

    email = f"db-{uuid.uuid4().hex[:8]}@test.com"
    db.add(User(email=email, password_hash="hashed"))
    await db.flush()

    from sqlalchemy import select

    found = await db.scalar(select(User).where(User.email == email))
    assert found is not None
    assert found.created_at is not None
