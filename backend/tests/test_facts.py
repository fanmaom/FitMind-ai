"""L2 事实记忆与向量召回。"""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import uuid

import httpx
import pytest
from httpx import ASGITransport, AsyncClient

from app.core.memory.embedding import EmbeddingError
from app.core.memory.facts import MAX_RECALL_DISTANCE, insert_fact, recall
from app.main import app

FACTS = [
    ("右肩有旧伤，做过顶推举时会疼", "injury"),
    ("公司楼下有家轻食店，鸡胸饭 32 块", "scenario"),
    ("不吃香菜", "preference"),
    ("喜欢早上空腹做有氧", "preference"),
    ("深蹲的个人最好成绩是 130kg", "performance"),
]


def _fake_vector(text: str) -> list[float]:
    """为语义簇生成正交单位向量，让 pgvector 的距离断言可重复。"""
    groups = [
        (0, ("肩", "推举")),
        (1, ("轻食", "鸡胸", "出差")),
        (2, ("香菜",)),
        (3, ("有氧",)),
        (4, ("深蹲", "训练")),
        (10, ("天气",)),
    ]
    index = 20
    for candidate, keywords in groups:
        if any(keyword in text for keyword in keywords):
            index = candidate
            break
    vector = [0.0] * 1536
    vector[index] = 1.0
    return vector


@pytest.fixture(autouse=True)
def _stub_fact_embeddings(monkeypatch):
    async def fake_embed_texts(texts: list[str]) -> list[list[float]]:
        return [_fake_vector(text) for text in texts]

    monkeypatch.setattr("app.core.memory.facts.embed_texts", fake_embed_texts)


@pytest.fixture
async def seeded_facts(db, seeded_user):
    for content, category in FACTS:
        await insert_fact(db, seeded_user, content, category, 0.9, None)
    await db.commit()
    return seeded_user


class TestRecall:
    @pytest.mark.asyncio
    async def test_semantic_recall_finds_related_fact(self, db, seeded_facts):
        hits = await recall(db, seeded_facts, "我肩膀不舒服，今天能练推举吗", k=3)
        assert hits
        assert "右肩" in hits[0].content

    @pytest.mark.asyncio
    async def test_respects_k(self, db, seeded_facts):
        assert len(await recall(db, seeded_facts, "训练", k=2)) <= 2

    @pytest.mark.asyncio
    async def test_distance_threshold_filters_noise(self, db, seeded_facts):
        hits = await recall(db, seeded_facts, "今天天气怎么样", k=5)
        assert len(hits) < len(FACTS), "毫不相关的查询召回了全部记忆"

    def test_threshold_is_configured_sanely(self):
        assert 0 < MAX_RECALL_DISTANCE < 1.0

    @pytest.mark.asyncio
    async def test_superseded_facts_excluded(self, db, seeded_user):
        old = await insert_fact(db, seeded_user, "右肩有旧伤", "injury", 0.9, None)
        new = await insert_fact(db, seeded_user, "右肩已经好了", "injury", 0.9, None)
        old.superseded_by = new.id
        await db.commit()

        contents = [m.content for m in await recall(db, seeded_user, "肩膀情况", k=5)]
        assert "右肩有旧伤" not in contents, "失效记忆仍被召回"

    @pytest.mark.asyncio
    async def test_expired_facts_excluded(self, db, seeded_user):
        memory = await insert_fact(db, seeded_user, "这周在出差", "scenario", 0.9, None)
        memory.expires_at = datetime.now(timezone.utc) - timedelta(days=1)
        await db.commit()

        hits = await recall(db, seeded_user, "出差", k=5)
        assert memory.content not in [item.content for item in hits]

    @pytest.mark.asyncio
    async def test_empty_store_returns_empty(self, db, seeded_user):
        assert await recall(db, seeded_user, "任何问题", k=5) == []

    @pytest.mark.asyncio
    async def test_embedding_failure_degrades_to_no_memory(self, db, seeded_user, monkeypatch):
        async def fail(_texts):
            raise EmbeddingError("offline")

        monkeypatch.setattr("app.core.memory.facts.embed_texts", fail)
        assert await recall(db, seeded_user, "肩膀", k=5) == []


class TestIsolation:
    @pytest.mark.asyncio
    async def test_cannot_recall_other_users_facts(self, db, seeded_facts):
        import uuid

        from sqlalchemy import text

        from app.core.database import bind_rls_user
        from app.models.user import User

        other = User(email=f"other-{uuid.uuid4().hex[:8]}@t.com", password_hash="x")
        db.add(other)
        await db.flush()
        bind_rls_user(db, other.id)
        await db.execute(
            text("SELECT set_config('app.user_id', :u, true)"),
            {"u": str(other.id)},
        )

        assert await recall(db, other.id, "肩膀", k=5) == []


class TestApi:
    @staticmethod
    def _headers(user_id) -> dict[str, str]:
        from app.core.security import create_access_token

        return {"Authorization": f"Bearer {create_access_token(str(user_id))}"}

    @pytest.mark.asyncio
    async def test_list_filter_and_delete(self, db, seeded_user):
        injury = await insert_fact(db, seeded_user, "右肩有旧伤", "injury", 0.9, None)
        await insert_fact(db, seeded_user, "不吃香菜", "preference", 0.8, None)
        await db.commit()

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            headers = self._headers(seeded_user)
            all_rows = (await client.get("/api/v1/memories", headers=headers)).json()
            assert {row["category"] for row in all_rows} == {"injury", "preference"}

            filtered = (
                await client.get("/api/v1/memories?category=injury", headers=headers)
            ).json()
            assert [row["id"] for row in filtered] == [str(injury.id)]

            response = await client.delete(
                f"/api/v1/memories/{injury.id}", headers=headers,
            )
            assert response.status_code == 204
            remaining = (await client.get("/api/v1/memories", headers=headers)).json()
            assert [row["category"] for row in remaining] == ["preference"]

    @pytest.mark.asyncio
    async def test_other_user_cannot_list_or_delete(self, db, seeded_user):
        memory = await insert_fact(db, seeded_user, "右肩有旧伤", "injury", 0.9, None)

        from app.models.user import User

        other = User(email=f"memory-api-{uuid.uuid4().hex[:8]}@t.com", password_hash="x")
        db.add(other)
        await db.flush()
        await db.commit()

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            headers = self._headers(other.id)
            assert (await client.get("/api/v1/memories", headers=headers)).json() == []
            response = await client.delete(
                f"/api/v1/memories/{memory.id}", headers=headers,
            )
            assert response.status_code == 404


class TestChatIntegration:
    @pytest.mark.asyncio
    async def test_recalled_facts_reach_context(self, monkeypatch):
        import app.services.chat_service as service

        captured = {}
        real_build_context = service.build_context

        async def fake_recall(*_args, **_kwargs):
            return [SimpleNamespace(content="右肩有旧伤")]

        def capturing_context(**kwargs):
            captured["facts"] = kwargs["facts"]
            return real_build_context(**kwargs)

        monkeypatch.setattr(service, "recall", fake_recall)
        monkeypatch.setattr(service, "build_context", capturing_context)
        monkeypatch.setattr(
            service,
            "build_provider",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("offline")),
        )

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            register = await client.post("/api/v1/auth/register", json={
                "email": f"facts-chat-{uuid.uuid4().hex[:8]}@t.com",
                "password": "pw123456",
            })
            headers = {"Authorization": f"Bearer {register.json()['access_token']}"}
            conversation = (
                await client.post("/api/v1/conversations", headers=headers)
            ).json()["id"]
            response = await client.post(
                f"/api/v1/conversations/{conversation}/messages",
                headers=headers,
                json={"text": "今天能练肩吗"},
            )
            assert response.status_code == 200

        assert captured["facts"] == ["右肩有旧伤"]


class _FakeResponse:
    def __init__(self, payload: dict):
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self._payload


class _FakeClient:
    last_request: dict | None = None

    def __init__(self, **_kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def post(self, url: str, **kwargs):
        type(self).last_request = {"url": url, **kwargs}
        texts = kwargs["json"]["input"]
        data = [
            {"index": index, "embedding": [float(index)] * 1536}
            for index in reversed(range(len(texts)))
        ]
        return _FakeResponse({"data": data})


class TestEmbedding:
    @pytest.mark.asyncio
    async def test_dimension_matches_config(self, monkeypatch):
        from app.core.config import get_settings
        from app.core.memory import embedding

        monkeypatch.setattr(embedding.httpx, "AsyncClient", _FakeClient)
        vecs = await embedding.embed_texts(["测试文本"])
        assert len(vecs[0]) == get_settings().embedding_dim

    @pytest.mark.asyncio
    async def test_batch_preserves_order(self, monkeypatch):
        from app.core.memory import embedding

        monkeypatch.setattr(embedding.httpx, "AsyncClient", _FakeClient)
        vecs = await embedding.embed_texts(["深蹲", "卧推", "硬拉"])
        assert [vector[0] for vector in vecs] == [0.0, 1.0, 2.0]

    @pytest.mark.asyncio
    async def test_empty_batch_skips_http(self, monkeypatch):
        from app.core.memory import embedding

        class ExplodingClient:
            def __init__(self, **_kwargs):
                raise AssertionError("空批次不应创建 HTTP client")

        monkeypatch.setattr(embedding.httpx, "AsyncClient", ExplodingClient)
        assert await embedding.embed_texts([]) == []

    @pytest.mark.asyncio
    async def test_http_error_is_wrapped(self, monkeypatch):
        from app.core.memory import embedding

        class FailingClient(_FakeClient):
            async def post(self, url: str, **kwargs):
                raise httpx.ConnectError("offline")

        monkeypatch.setattr(embedding.httpx, "AsyncClient", FailingClient)
        with pytest.raises(EmbeddingError, match="embedding 服务不可用"):
            await embedding.embed_texts(["测试"])
