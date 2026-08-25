"""PostgreSQL 异步任务队列。"""

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from app.core.jobs.queue import MAX_ATTEMPTS, claim_jobs, complete, enqueue, fail


class TestEnqueueAndClaim:
    @pytest.mark.asyncio
    async def test_enqueue_then_claim(self, db, seeded_user):
        await enqueue(db, "extract_memory", {"message_id": "abc"})
        await db.commit()

        jobs = await claim_jobs(db, limit=10)
        assert len(jobs) == 1
        assert jobs[0].type == "extract_memory"
        assert jobs[0].payload["message_id"] == "abc"

    @pytest.mark.asyncio
    async def test_claimed_job_not_returned_twice(self, db, seeded_user):
        await enqueue(db, "extract_memory", {"n": 1})
        await db.commit()

        first = await claim_jobs(db, limit=10)
        await complete(db, first[0])
        await db.commit()
        assert await claim_jobs(db, limit=10) == []

    @pytest.mark.asyncio
    async def test_future_jobs_not_claimed(self, db, seeded_user):
        job = await enqueue(db, "extract_memory", {"n": 1})
        job.run_after = datetime.now(timezone.utc) + timedelta(hours=1)
        await db.commit()
        assert await claim_jobs(db, limit=10) == []

    @pytest.mark.asyncio
    async def test_enqueue_without_rls_identity_is_rejected(self, db):
        with pytest.raises(ValueError, match="user_id"):
            await enqueue(db, "extract_memory", {})


class TestConcurrency:
    @pytest.mark.asyncio
    async def test_skip_locked_does_not_wait_for_other_worker(self, db, seeded_user):
        """第二个 worker 应立即跳过锁定行；去掉 SKIP LOCKED 会在此超时。"""
        from app.core.database import async_session_maker, bind_rls_user

        await enqueue(db, "extract_memory", {"n": 1})
        await db.commit()

        locked = asyncio.Event()
        release = asyncio.Event()

        async def holder() -> None:
            session = async_session_maker()
            bind_rls_user(session, seeded_user)
            try:
                assert await claim_jobs(session, limit=1)
                locked.set()
                await release.wait()
                await session.rollback()
            finally:
                await session.close()

        first = asyncio.create_task(holder())
        await locked.wait()

        second_session = async_session_maker()
        bind_rls_user(second_session, seeded_user)
        try:
            second = await asyncio.wait_for(claim_jobs(second_session, limit=1), timeout=0.15)
            assert second == []
        finally:
            release.set()
            await second_session.rollback()
            await second_session.close()
            await first


class TestRetry:
    @pytest.mark.asyncio
    async def test_failure_increments_attempts_and_reschedules(self, db, seeded_user):
        await enqueue(db, "extract_memory", {"n": 1})
        await db.commit()

        job = (await claim_jobs(db, limit=1))[0]
        await fail(db, job, "网络超时")
        await db.commit()

        assert job.attempts == 1
        assert job.status == "pending"
        assert job.last_error == "网络超时"
        assert job.run_after > datetime.now(timezone.utc)

    @pytest.mark.asyncio
    async def test_gives_up_after_max_attempts(self, db, seeded_user):
        await enqueue(db, "extract_memory", {"n": 1})
        await db.commit()

        job = (await claim_jobs(db, limit=1))[0]
        for index in range(MAX_ATTEMPTS):
            job.run_after = datetime.now(timezone.utc)
            await fail(db, job, f"第 {index + 1} 次失败")
        await db.commit()

        assert job.attempts == MAX_ATTEMPTS
        assert job.status == "failed"
        assert job.last_error == f"第 {MAX_ATTEMPTS} 次失败"

    @pytest.mark.asyncio
    async def test_error_is_bounded(self, db, seeded_user):
        await enqueue(db, "extract_memory", {})
        await db.commit()
        job = (await claim_jobs(db, limit=1))[0]
        await fail(db, job, "x" * 3000)
        assert len(job.last_error) == 2000


class TestChatEnqueue:
    @pytest.mark.asyncio
    async def test_normal_reply_enqueues_after_completion(self, monkeypatch):
        import uuid

        from httpx import ASGITransport, AsyncClient
        from sqlalchemy import select

        import app.services.chat_service as service
        from app.core.database import async_session_maker, bind_rls_user
        from app.core.security import decode_token
        from app.main import app
        from app.models.job import Job

        monkeypatch.setattr(
            service,
            "build_provider",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("offline")),
        )

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            registered = await client.post("/api/v1/auth/register", json={
                "email": f"job-chat-{uuid.uuid4().hex[:8]}@t.com",
                "password": "pw123456",
            })
            token = registered.json()["access_token"]
            headers = {"Authorization": f"Bearer {token}"}
            conversation_id = (
                await client.post("/api/v1/conversations", headers=headers)
            ).json()["id"]
            response = await client.post(
                f"/api/v1/conversations/{conversation_id}/messages",
                headers=headers,
                json={"text": "我不吃香菜"},
            )
            assert response.status_code == 200
            assert "message_done" in response.text

        user_id = uuid.UUID(decode_token(token))
        session = async_session_maker()
        bind_rls_user(session, user_id)
        try:
            jobs = (await session.scalars(select(Job))).all()
            assert len(jobs) == 1
            assert jobs[0].type == "extract_memory"
            assert "我不吃香菜" in jobs[0].payload["conversation_text"]
        finally:
            await session.close()
