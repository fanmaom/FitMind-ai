"""RLS 租户隔离测试。

核心一条：隔离测试必须并发。SET（会话级）泄漏只在连接池复用连接时暴露，
顺序执行的测试永远是绿的——那种绿是假的。
"""

import asyncio
import uuid

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from app.main import app


async def _register(client: AsyncClient, tag: str) -> str:
    r = await client.post(
        "/api/v1/auth/register",
        json={"email": f"{tag}-{uuid.uuid4().hex[:8]}@test.com", "password": "pw123456"},
    )
    assert r.status_code == 201, r.text
    return r.json()["access_token"]


@pytest.mark.asyncio
async def test_concurrent_users_cannot_see_each_others_logs():
    """并发发起 A、B 两个用户的请求，断言互相拿不到数据。"""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        token_a = await _register(client, "rls-a")
        token_b = await _register(client, "rls-b")
        head_a = {"Authorization": f"Bearer {token_a}"}
        head_b = {"Authorization": f"Bearer {token_b}"}

        r = await client.post("/api/v1/logs/workouts", headers=head_a, json={
            "date": "2026-08-24", "exercise": "深蹲", "sets": [{"weight": 120, "reps": 5}],
        })
        assert r.status_code == 201, r.text
        r = await client.post("/api/v1/logs/workouts", headers=head_b, json={
            "date": "2026-08-24", "exercise": "卧推", "sets": [{"weight": 80, "reps": 5}],
        })
        assert r.status_code == 201, r.text

        async def fetch(headers: dict) -> list[dict]:
            resp = await client.get("/api/v1/logs/workouts", headers=headers)
            assert resp.status_code == 200, resp.text
            return resp.json()

        # 并发 20 轮交替，逼出连接池复用
        results = await asyncio.gather(
            *[fetch(head_a if i % 2 == 0 else head_b) for i in range(20)],
        )

        for i, rows in enumerate(results):
            exercises = {row["exercise"] for row in rows}
            if i % 2 == 0:
                assert exercises == {"深蹲"}, f"第 {i} 轮 A 看到了不属于他的数据：{exercises}"
            else:
                assert exercises == {"卧推"}, f"第 {i} 轮 B 看到了不属于他的数据：{exercises}"


@pytest.mark.asyncio
async def test_raw_query_without_where_still_isolated(db):
    """即使 SQL 忘了写 where user_id，RLS 也必须挡住。"""
    await db.execute(
        text("SELECT set_config('app.user_id', :uid, true)"),
        {"uid": "00000000-0000-0000-0000-000000000000"},
    )
    result = await db.execute(text("SELECT count(*) FROM workout_logs"))
    assert result.scalar_one() == 0


@pytest.mark.asyncio
async def test_no_context_means_no_rows(db):
    """未设置上下文时必须什么都读不到（fail-safe），而不是读到全部。"""
    result = await db.execute(text("SELECT count(*) FROM workout_logs"))
    assert result.scalar_one() == 0


@pytest.mark.asyncio
async def test_setting_cleared_after_commit_not_just_rollback():
    """set_config(..., true) 必须在 COMMIT 后也失效。

    这里必须用 commit 而不是 rollback——这是本测试全部的意义所在：
    会话级 SET 若发生在一个被 ROLLBACK 的事务里，会跟着事务一起回滚，
    于是 true / false 两种写法在 rollback 路径上表现完全一致，测不出差别。
    泄漏只在 COMMIT 之后才暴露：提交过的会话级设置会留在连接上，
    被下一个从池里拿到这条连接的请求继承。
    """
    from app.core.database import engine

    async with engine.connect() as conn:
        await conn.execute(
            text("SELECT set_config('app.user_id', :uid, true)"),
            {"uid": str(uuid.uuid4())},
        )
        inside = (await conn.execute(text("SELECT current_setting('app.user_id', true)"))).scalar_one()
        assert inside, "事务内应当读到刚设的值"

        await conn.commit()

        after = (await conn.execute(text("SELECT current_setting('app.user_id', true)"))).scalar_one()
        assert not after, (
            "COMMIT 后 app.user_id 仍有值——说明用的是会话级 SET 而非事务级 "
            "SET LOCAL/set_config(...,true)，该值会随连接池泄漏给下一个用户"
        )


@pytest.mark.asyncio
async def test_pooled_connection_carries_no_context_to_unauthenticated_path():
    """已认证请求用完的连接被复用后，不设上下文的代码路径必须读不到任何数据。

    这是会话级泄漏真正的杀伤场景：某条不经过 get_context 的路径（后台任务、
    忘了加依赖的接口）拿到一条刚服务过用户 A 的池化连接，于是读到 A 的数据。
    并发接口测试测不出这个——每个请求都会先过 get_context 覆盖掉残留值。

    注意本测试全程不 dispose 连接池，否则复用不会发生，就测不到东西了。
    """
    from app.core.database import async_session_maker

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        token = await _register(client, "leak")
        headers = {"Authorization": f"Bearer {token}"}
        r = await client.post("/api/v1/logs/workouts", headers=headers, json={
            "date": "2026-08-24", "exercise": "过顶推举", "sets": [{"weight": 50, "reps": 8}],
        })
        assert r.status_code == 201, r.text

    # 连接已归还池中。开多个 session 复用它们，任何一个都不该看到数据。
    for attempt in range(10):
        session = async_session_maker()
        try:
            leaked = (await session.execute(
                text("SELECT current_setting('app.user_id', true)"),
            )).scalar_one()
            assert not leaked, (
                f"第 {attempt} 次取到的池化连接仍带着 app.user_id={leaked!r}，"
                f"上一个用户的身份泄漏了"
            )
            count = (await session.execute(text("SELECT count(*) FROM workout_logs"))).scalar_one()
            assert count == 0, f"第 {attempt} 次在无上下文的 session 里读到了 {count} 行数据"
        finally:
            await session.rollback()
            await session.close()


@pytest.mark.asyncio
async def test_rls_is_forced_on_tenant_tables(db):
    """表 owner 默认绕过 RLS，必须 FORCE，否则 policy 形同虚设。"""
    row = (await db.execute(text(
        "SELECT relrowsecurity, relforcerowsecurity FROM pg_class WHERE relname = 'workout_logs'",
    ))).one()
    assert row.relrowsecurity is True, "workout_logs 未启用 RLS"
    assert row.relforcerowsecurity is True, "workout_logs 未 FORCE RLS，owner 会绕过策略"


@pytest.mark.asyncio
async def test_duplicate_workout_is_deduplicated():
    """重试机制遇上写操作，不做幂等就会一次训练记两条。"""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        token = await _register(client, "idem")
        headers = {"Authorization": f"Bearer {token}"}
        body = {"date": "2026-08-24", "exercise": "硬拉", "sets": [{"weight": 140, "reps": 3}]}

        r1 = await client.post("/api/v1/logs/workouts", headers=headers, json=body)
        r2 = await client.post("/api/v1/logs/workouts", headers=headers, json=body)

        assert r1.json()["deduplicated"] is False
        assert r2.json()["deduplicated"] is True

        rows = (await client.get("/api/v1/logs/workouts", headers=headers)).json()
        assert len([r for r in rows if r["exercise"] == "硬拉"]) == 1
