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


class TestMultiQueryRecall:
    """多路检索：命中任意一路即可。

    续问句自身没有可检索的实体，靠拼上文的第二路把它救回来。
    """

    @pytest.mark.asyncio
    async def test_second_query_rescues_what_first_misses(self, db, seeded_facts):
        """这条是整个改动的命题：单查询召不回，补一路上文就能召回。"""
        bare = await recall(db, seeded_facts, "那个呢", k=5)
        assert not bare, "无实体的续问句居然召回了内容，测试样本失去意义"

        both = await recall(db, seeded_facts, ["那个呢", "公司楼下轻食店"], k=5)
        assert [m.content for m in both], "补了上文那一路仍然召不回"
        assert any("轻食店" in m.content for m in both)

    @pytest.mark.asyncio
    async def test_first_query_hits_are_kept(self, db, seeded_facts):
        """补第二路不能挤掉第一路本来召得回的。"""
        alone = {m.content for m in await recall(db, seeded_facts, "肩膀疼", k=5)}
        assert alone
        together = {
            m.content for m in await recall(db, seeded_facts, ["肩膀疼", "香菜"], k=5)
        }
        assert alone <= together

    @pytest.mark.asyncio
    async def test_adding_query_never_loses_recall(self, db, seeded_facts):
        """多路取最小距离，所以在 k 够用时**严格不劣于**单路——这是选双路而不选
        拼接改写的根本理由。拼接是把两路信息压成一条，会稀释原句关键词：实测
        「那午饭呢？」原句距离 0.479 召得回，拼上上文变 0.641 反而召不回。

        这条断言守的就是那个性质：加一路只可能多召回，不可能少召回。
        （k 被占满时新命中仍可能挤掉旧的，那是 k 的语义而非策略缺陷，
        由 test_k_still_caps_total 覆盖。这里用 k=len(FACTS) 排除该干扰。）
        """
        k = len(FACTS)
        for extra in ("香菜", "深蹲成绩", "今天天气怎么样", "毫不相关的内容"):
            base = {m.content for m in await recall(db, seeded_facts, "肩膀疼", k=k)}
            widened = {
                m.content
                for m in await recall(db, seeded_facts, ["肩膀疼", extra], k=k)
            }
            assert base <= widened, f"加入第二路「{extra}」后丢了原本召得回的事实"

    @pytest.mark.asyncio
    async def test_no_duplicates_when_both_queries_hit_same_fact(self, db, seeded_facts):
        hits = await recall(db, seeded_facts, ["肩膀疼", "推举时肩膀疼"], k=5)
        contents = [m.content for m in hits]
        assert len(contents) == len(set(contents)), "同一条记忆被两路各返回了一次"

    @pytest.mark.asyncio
    async def test_k_still_caps_total(self, db, seeded_facts):
        """k 是全局名额，不是每路各 k 条。"""
        hits = await recall(db, seeded_facts, ["肩膀疼", "香菜", "深蹲成绩"], k=2)
        assert len(hits) <= 2

    @pytest.mark.asyncio
    async def test_threshold_still_applies_to_every_query(self, db, seeded_facts):
        """多路不能变成绕过阈值的后门。"""
        hits = await recall(db, seeded_facts, ["今天天气怎么样", "明天天气怎么样"], k=5)
        assert len(hits) < len(FACTS)

    @pytest.mark.asyncio
    async def test_embeddings_fetched_in_one_call(self, db, seeded_facts, monkeypatch):
        """多路检索不该变成多次网络往返——这条召回在用户等回复的主链路上。"""
        calls = []

        async def counting_embed(texts):
            calls.append(list(texts))
            return [_fake_vector(text) for text in texts]

        monkeypatch.setattr("app.core.memory.facts.embed_texts", counting_embed)
        await recall(db, seeded_facts, ["肩膀疼", "香菜", "深蹲"], k=5)
        assert len(calls) == 1, f"发起了 {len(calls)} 次 embedding 调用"
        assert len(calls[0]) == 3

    @pytest.mark.asyncio
    async def test_blank_queries_are_dropped(self, db, seeded_facts):
        hits = await recall(db, seeded_facts, ["肩膀疼", "  ", ""], k=5)
        assert any("右肩" in m.content for m in hits)

    @pytest.mark.asyncio
    async def test_all_blank_returns_empty_without_calling_embedding(
        self, db, seeded_facts, monkeypatch,
    ):
        async def exploding(_texts):
            raise AssertionError("全空 query 不该发起 embedding 调用")

        monkeypatch.setattr("app.core.memory.facts.embed_texts", exploding)
        assert await recall(db, seeded_facts, ["", "   "], k=5) == []

    @pytest.mark.asyncio
    async def test_single_string_query_still_works(self, db, seeded_facts):
        """兼容原调用形态。"""
        hits = await recall(db, seeded_facts, "肩膀疼", k=5)
        assert any("右肩" in m.content for m in hits)


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

    @staticmethod
    async def _run_turns(client, texts: list[str]) -> str:
        register = await client.post("/api/v1/auth/register", json={
            "email": f"ctxq-{uuid.uuid4().hex[:8]}@t.com",
            "password": "pw123456",
        })
        headers = {"Authorization": f"Bearer {register.json()['access_token']}"}
        conversation = (
            await client.post("/api/v1/conversations", headers=headers)
        ).json()["id"]
        for text in texts:
            response = await client.post(
                f"/api/v1/conversations/{conversation}/messages",
                headers=headers, json={"text": text},
            )
            assert response.status_code == 200
        return conversation

    @pytest.fixture
    def _offline_llm(self, monkeypatch):
        """让回合走 L4/L5，不打真实网关；召回逻辑仍然完整执行。"""
        import app.services.chat_service as service

        monkeypatch.setattr(
            service, "build_provider",
            lambda *_a, **_kw: (_ for _ in ()).throw(RuntimeError("offline")),
        )

    @pytest.mark.asyncio
    async def test_followup_turn_recalls_with_context_query(
        self, monkeypatch, _offline_llm,
    ):
        """续问句必须带上文去检索，否则记忆系统在多轮里集体失灵。"""
        import app.services.chat_service as service

        seen: list[list[str]] = []

        async def capturing_recall(_session, _user_id, query, k=5):
            seen.append([query] if isinstance(query, str) else list(query))
            return []

        monkeypatch.setattr(service, "recall", capturing_recall)

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            await self._run_turns(client, ["帮我做个 8 周减脂计划", "那午饭呢？"])

        first, second = seen[0], seen[-1]
        assert first == ["帮我做个 8 周减脂计划"], "自足的首句不该触发第二路"
        assert len(second) == 2, "续问句没有启用上文补充检索"
        assert second[0] == "那午饭呢？", "原句必须保留为第一路——它是不劣性的来源"
        assert "减脂计划" in second[1], "第二路没有带上上一轮的内容"
        assert "那午饭呢" not in second[1], "上文那一路不该掺进当前句"

    @pytest.mark.asyncio
    async def test_self_contained_followup_stays_single_query(
        self, monkeypatch, _offline_llm,
    ):
        """语义自足的后续提问不拼上文——掺进无关内容会把 query 向量拉偏。"""
        import app.services.chat_service as service

        seen: list[list[str]] = []

        async def capturing_recall(_session, _user_id, query, k=5):
            seen.append([query] if isinstance(query, str) else list(query))
            return []

        monkeypatch.setattr(service, "recall", capturing_recall)

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            await self._run_turns(
                client, ["帮我做个 8 周减脂计划", "我右肩有旧伤，能练推举吗"],
            )

        assert seen[-1] == ["我右肩有旧伤，能练推举吗"]


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
