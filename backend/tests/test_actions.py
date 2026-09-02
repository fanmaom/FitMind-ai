"""待办（教练建议沉淀）：抽取解析、向量去重与 API。"""

import uuid

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.core.actions.dedupe import insert_if_new
from app.core.actions.extractor import (
    MAX_ITEMS_PER_TURN,
    handle_extract_actions,
)
from app.core.actions.extractor import _parse as parse_actions
from app.core.actions.store import CANDIDATE_DISTANCE, insert_action
from app.core.config import get_settings
from app.main import app
from app.models.action_item import ActionItem

_DIMENSION = get_settings().embedding_dim

# 实测的真实余弦距离，用来把「向量单独判不了重」这个结论钉住。
# 见 store.CANDIDATE_DISTANCE 的注释。
MEASURED_SAME_TASK_FARTHEST = 0.314   # 同一件事：kg → 公斤，加时间词
MEASURED_DIFF_TASK_NEAREST = 0.254    # 不同动作的同款建议：深蹲加重 vs 卧推加重
MEASURED_UNRELATED_NEAREST = 0.742    # 不同类别：补蛋白 vs 卧推加重


def _fake_vector(text: str) -> list[float]:
    """同文本同向量、异文本近正交，让候选召回在测试里行为确定。"""
    vector = [0.0] * _DIMENSION
    vector[hash(text) % _DIMENSION] = 1.0
    return vector


@pytest.fixture
def fake_embeddings(monkeypatch):
    async def embed(texts: list[str]) -> list[list[float]]:
        return [_fake_vector(text) for text in texts]

    monkeypatch.setattr("app.core.actions.store.embed_texts", embed)


@pytest.fixture
def judge_says_same(monkeypatch):
    """判重 LLM 一律判「同一件事」。"""
    async def judge(_existing: str, _candidate: str) -> bool:
        return True

    monkeypatch.setattr("app.core.actions.dedupe._is_same_task", judge)


@pytest.fixture
def judge_says_different(monkeypatch):
    async def judge(_existing: str, _candidate: str) -> bool:
        return False

    monkeypatch.setattr("app.core.actions.dedupe._is_same_task", judge)


async def _auth(client: AsyncClient, tag: str) -> dict:
    r = await client.post("/api/v1/auth/register", json={
        "email": f"{tag}-{uuid.uuid4().hex[:8]}@t.com", "password": "pw123456"})
    assert r.status_code == 201, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


class TestParse:
    def test_parses_well_formed_array(self):
        actions = parse_actions(
            '[{"content": "把卧推加到 82.5kg", "category": "training", '
            '"quote": "下周可以把卧推加到 82.5kg", "confidence": 0.9}]',
        )
        assert len(actions) == 1
        assert actions[0].content == "把卧推加到 82.5kg"
        assert actions[0].category == "training"
        assert actions[0].quote == "下周可以把卧推加到 82.5kg"
        assert actions[0].confidence == 0.9

    def test_strips_code_fence(self):
        actions = parse_actions(
            '```json\n[{"content": "补到 140g 蛋白", "category": "nutrition", '
            '"confidence": 0.8}]\n```',
        )
        assert len(actions) == 1
        assert actions[0].category == "nutrition"

    def test_garbage_yields_empty_not_crash(self):
        """模型偶尔会返回散文。抽取是异步的旁路，绝不能因此让整个 job 炸掉。"""
        assert parse_actions("我觉得你应该多练腿。") == []
        assert parse_actions("") == []
        assert parse_actions('{"content": "不是数组"}') == []

    def test_unknown_category_falls_back_to_general(self):
        actions = parse_actions(
            '[{"content": "x", "category": "玄学", "confidence": 1}]',
        )
        assert actions[0].category == "general"

    def test_missing_content_skipped(self):
        actions = parse_actions(
            '[{"category": "training", "confidence": 1}, '
            '{"content": "有效", "confidence": 1}]',
        )
        assert [a.content for a in actions] == ["有效"]

    def test_non_numeric_confidence_skipped(self):
        actions = parse_actions('[{"content": "x", "confidence": "高"}]')
        assert actions == []

    def test_confidence_clamped(self):
        actions = parse_actions(
            '[{"content": "a", "confidence": 5}, {"content": "b", "confidence": -2}]',
        )
        assert actions[0].confidence == 1.0
        assert actions[1].confidence == 0.0

    def test_truncates_beyond_max_items(self):
        """模型无视 max_items 是常事；一轮冒出十条待办面板就没法用了。"""
        raw = ",".join(
            f'{{"content": "第{i}条", "confidence": 1}}' for i in range(10)
        )
        assert len(parse_actions(f"[{raw}]")) == MAX_ITEMS_PER_TURN


class TestThresholdGeometry:
    """把「向量单独判不了重」这个实测结论钉成断言。

    这不是在测代码，是在测那个常量的取值前提。哪天有人想把 CANDIDATE_DISTANCE
    收紧回 0.3 当判重线用，这几条会先炸。
    """

    def test_candidate_gate_admits_all_true_duplicates(self):
        assert MEASURED_SAME_TASK_FARTHEST < CANDIDATE_DISTANCE, (
            "最远的真重复被粗筛挡在门外，判重根本轮不到 LLM"
        )

    def test_candidate_gate_excludes_unrelated(self):
        assert CANDIDATE_DISTANCE < MEASURED_UNRELATED_NEAREST, (
            "无关建议也进候选，白烧 LLM 调用"
        )

    def test_no_threshold_can_separate_same_from_different(self):
        """区间重叠，所以必须有 LLM 判定这一步，不能只靠阈值。"""
        assert MEASURED_DIFF_TASK_NEAREST < MEASURED_SAME_TASK_FARTHEST, (
            "样本区间不再重叠——若真如此，纯阈值判重可行，本模块可以简化"
        )


class TestDedupe:
    @pytest.mark.asyncio
    async def test_identical_suggestion_inserted_once(
        self, db, seeded_user, fake_embeddings, judge_says_same,
    ):
        first = await insert_if_new(db, seeded_user, "把卧推加到 82.5kg", "training")
        second = await insert_if_new(db, seeded_user, "把卧推加到 82.5kg", "training")
        assert first is not None
        assert second is None, "重复建议应被拦下"

    @pytest.mark.asyncio
    async def test_near_neighbour_kept_when_judge_says_different(
        self, db, seeded_user, fake_embeddings, judge_says_different,
    ):
        """深蹲/卧推那类「同模板不同动作」必须两条都留下。

        实测它们的向量距离（0.254）比真重复（0.314）还近，纯阈值会把后来的那条
        悄悄吞掉——用户永远不知道助理建议过深蹲加重。
        """
        assert await insert_if_new(db, seeded_user, "把卧推加到 82.5kg", "training")
        assert await insert_if_new(db, seeded_user, "把深蹲加到 120kg", "training")

    @pytest.mark.asyncio
    async def test_judge_failure_keeps_the_item(
        self, db, seeded_user, fake_embeddings, monkeypatch,
    ):
        """判定失败不能静默丢建议——用户看不到的东西无法自己纠正。"""
        async def broken(_existing: str, _candidate: str) -> bool:
            raise RuntimeError("网关挂了")

        assert await insert_if_new(db, seeded_user, "把卧推加到 82.5kg", "training")
        monkeypatch.setattr("app.core.actions.dedupe._is_same_task", broken)
        assert await insert_if_new(db, seeded_user, "把卧推加到 82.5kg", "training")

    @pytest.mark.asyncio
    async def test_unrelated_suggestion_skips_judge_entirely(
        self, db, seeded_user, fake_embeddings, monkeypatch,
    ):
        """向量粗筛该把无关项挡住，不该为它烧一次 LLM 调用。"""
        calls: list[tuple[str, str]] = []

        async def counting_judge(existing: str, candidate: str) -> bool:
            calls.append((existing, candidate))
            return False

        monkeypatch.setattr("app.core.actions.dedupe._is_same_task", counting_judge)
        await insert_if_new(db, seeded_user, "把卧推加到 82.5kg", "training")
        # _fake_vector 让不同文本近正交（距离 1.0），远超 CANDIDATE_DISTANCE
        await insert_if_new(db, seeded_user, "蛋白补到 140g", "nutrition")
        assert calls == []

    @pytest.mark.asyncio
    async def test_ignored_item_still_blocks_readd(
        self, db, seeded_user, fake_embeddings, judge_says_same,
    ):
        """用户明确划掉的建议，下一轮再冒出来就是骚扰。"""
        item = await insert_if_new(db, seeded_user, "每天早睡", "recovery")
        assert item is not None
        item.status = "ignored"
        await db.flush()

        assert await insert_if_new(db, seeded_user, "每天早睡", "recovery") is None

    @pytest.mark.asyncio
    async def test_done_item_allows_readd(
        self, db, seeded_user, fake_embeddings, judge_says_same,
    ):
        """做完的事下个周期该再提一次，否则周期性任务只会出现一次。"""
        item = await insert_if_new(db, seeded_user, "把卧推加到 82.5kg", "training")
        assert item is not None
        item.status = "done"
        await db.flush()

        assert await insert_if_new(db, seeded_user, "把卧推加到 82.5kg", "training")

    @pytest.mark.asyncio
    async def test_manual_insert_skips_dedupe(
        self, db, seeded_user, fake_embeddings, judge_says_same,
    ):
        """用户亲手打出来的条目，系统不该替他判重。"""
        assert await insert_if_new(db, seeded_user, "买乳清蛋白", "nutrition")
        assert await insert_action(db, seeded_user, "买乳清蛋白", "nutrition")

    @pytest.mark.asyncio
    async def test_dedupe_is_scoped_to_one_user(
        self, db, seeded_user, fake_embeddings, judge_says_same,
    ):
        """候选查询自己带 user_id，不靠 RLS 兜底。"""
        assert await insert_if_new(db, seeded_user, "把卧推加到 82.5kg", "training")
        rows = (await db.scalars(
            select(ActionItem).where(ActionItem.user_id != seeded_user),
        )).all()
        assert rows == []


class TestExtractHandler:
    @pytest.mark.asyncio
    async def test_low_confidence_dropped(
        self, db, seeded_user, fake_embeddings, monkeypatch,
    ):
        from app.core.actions.extractor import ExtractedAction

        async def fake_extract(_text: str) -> list[ExtractedAction]:
            return [
                ExtractedAction("留下来", "training", "原话", 0.9),
                ExtractedAction("丢掉", "training", None, 0.3),
            ]

        monkeypatch.setattr(
            "app.core.actions.extractor.extract_actions", fake_extract,
        )
        message_id, conversation_id = uuid.uuid4(), uuid.uuid4()
        await handle_extract_actions(db, {
            "user_id": str(seeded_user),
            "conversation_text": "随便",
            "source_message_id": str(message_id),
            "source_conversation_id": str(conversation_id),
        })

        rows = (await db.scalars(select(ActionItem))).all()
        assert [row.content for row in rows] == ["留下来"]
        assert rows[0].source_message_id == message_id
        assert rows[0].source_conversation_id == conversation_id
        assert rows[0].source_quote == "原话"
        assert rows[0].status == "pending"

    @pytest.mark.asyncio
    async def test_missing_source_ids_tolerated(
        self, db, seeded_user, fake_embeddings, monkeypatch,
    ):
        from app.core.actions.extractor import ExtractedAction

        async def fake_extract(_text: str) -> list[ExtractedAction]:
            return [ExtractedAction("无来源", "general", None, 1.0)]

        monkeypatch.setattr(
            "app.core.actions.extractor.extract_actions", fake_extract,
        )
        await handle_extract_actions(db, {
            "user_id": str(seeded_user), "conversation_text": "x",
        })
        rows = (await db.scalars(select(ActionItem))).all()
        assert rows[0].source_message_id is None


class TestWorkerRegistration:
    def test_extract_actions_is_dispatchable(self):
        """worker 靠 HANDLERS 分发；漏注册的话任务会永远重试到 failed。"""
        from app.core.jobs.worker import HANDLERS

        assert "extract_actions" in HANDLERS
        assert "extract_memory" in HANDLERS, "不该动到原有的记忆抽取"


class TestApi:
    @pytest.mark.asyncio
    async def test_create_list_complete_reopen(self, fake_embeddings):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            headers = await _auth(client, "act")

            created = await client.post("/api/v1/action-items", headers=headers,
                                        json={"content": "买乳清蛋白"})
            assert created.status_code == 201, created.text
            item = created.json()
            assert item["status"] == "pending"
            assert item["settled_at"] is None

            listed = await client.get("/api/v1/action-items", headers=headers)
            assert [row["content"] for row in listed.json()] == ["买乳清蛋白"]

            done = await client.patch(f"/api/v1/action-items/{item['id']}",
                                      headers=headers, json={"status": "done"})
            assert done.status_code == 200
            assert done.json()["status"] == "done"
            assert done.json()["settled_at"] is not None

            reopened = await client.patch(f"/api/v1/action-items/{item['id']}",
                                          headers=headers, json={"status": "pending"})
            assert reopened.json()["settled_at"] is None, (
                "重新打开后仍带完成时间，面板会显示一条既未完成又已完成的条目"
            )

    @pytest.mark.asyncio
    async def test_status_filter(self, fake_embeddings):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            headers = await _auth(client, "act-filter")
            a = (await client.post("/api/v1/action-items", headers=headers,
                                   json={"content": "第一条"})).json()
            await client.post("/api/v1/action-items", headers=headers,
                              json={"content": "第二条"})
            await client.patch(f"/api/v1/action-items/{a['id']}",
                               headers=headers, json={"status": "ignored"})

            pending = await client.get("/api/v1/action-items?status=pending",
                                       headers=headers)
            assert [row["content"] for row in pending.json()] == ["第二条"]

            ignored = await client.get("/api/v1/action-items?status=ignored",
                                       headers=headers)
            assert [row["content"] for row in ignored.json()] == ["第一条"]

    @pytest.mark.asyncio
    async def test_rejects_bad_input(self, fake_embeddings):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            headers = await _auth(client, "act-bad")

            assert (await client.post("/api/v1/action-items", headers=headers,
                                      json={"content": "   "})).status_code == 422
            assert (await client.get("/api/v1/action-items?status=玄学",
                                     headers=headers)).status_code == 422

            item = (await client.post("/api/v1/action-items", headers=headers,
                                      json={"content": "x"})).json()
            assert (await client.patch(f"/api/v1/action-items/{item['id']}",
                                       headers=headers,
                                       json={"status": "玄学"})).status_code == 422

    @pytest.mark.asyncio
    async def test_delete_then_missing(self, fake_embeddings):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            headers = await _auth(client, "act-del")
            item = (await client.post("/api/v1/action-items", headers=headers,
                                      json={"content": "删我"})).json()

            assert (await client.delete(f"/api/v1/action-items/{item['id']}",
                                        headers=headers)).status_code == 204
            assert (await client.delete(f"/api/v1/action-items/{item['id']}",
                                        headers=headers)).status_code == 404
            assert (await client.patch(f"/api/v1/action-items/{item['id']}",
                                       headers=headers,
                                       json={"status": "done"})).status_code == 404

    @pytest.mark.asyncio
    async def test_isolated_between_users(self, fake_embeddings):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            head_a = await _auth(client, "act-iso-a")
            head_b = await _auth(client, "act-iso-b")
            item = (await client.post("/api/v1/action-items", headers=head_a,
                                      json={"content": "A 的待办"})).json()

            assert (await client.get("/api/v1/action-items", headers=head_b)).json() == []
            assert (await client.patch(f"/api/v1/action-items/{item['id']}",
                                       headers=head_b,
                                       json={"status": "done"})).status_code == 404
            assert (await client.delete(f"/api/v1/action-items/{item['id']}",
                                        headers=head_b)).status_code == 404

    @pytest.mark.asyncio
    async def test_embedding_outage_returns_503(self, monkeypatch):
        """向量列 NOT NULL，拿不到向量就写不进去。这是依赖不可用，重试有意义。"""
        from app.core.memory.embedding import EmbeddingError

        async def dead(_texts):
            raise EmbeddingError("网关挂了")

        monkeypatch.setattr("app.core.actions.store.embed_texts", dead)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            headers = await _auth(client, "act-503")
            r = await client.post("/api/v1/action-items", headers=headers,
                                  json={"content": "x"})
            assert r.status_code == 503


class TestChatEnqueue:
    """回合收尾必须同时投两个抽取任务，且中断的回合一个都不投。"""

    @staticmethod
    def _spy(monkeypatch) -> list[tuple[str, dict]]:
        import app.services.chat_service as svc

        posted: list[tuple[str, dict]] = []

        async def fake_enqueue(_session, job_type, payload, **_kwargs):
            posted.append((job_type, payload))

        monkeypatch.setattr(svc, "enqueue", fake_enqueue)
        return posted

    @pytest.mark.asyncio
    async def test_turn_enqueues_both_extractions(self, monkeypatch):
        import app.services.chat_service as svc

        # LLM 构造失败 → 走 L4 规则兜底，回合照样正常收尾。
        monkeypatch.setattr(
            svc, "build_provider",
            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("dead")),
        )
        posted = self._spy(monkeypatch)

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            headers = await _auth(client, "act-enq")
            conv = (await client.post("/api/v1/conversations",
                                      headers=headers)).json()["id"]
            r = await client.post(f"/api/v1/conversations/{conv}/messages",
                                  headers=headers, json={"text": "我该吃多少"})
            assert r.status_code == 200, r.text

        assert [job_type for job_type, _ in posted] == [
            "extract_memory", "extract_actions",
        ]

        memory_payload = dict(posted)["extract_memory"]
        actions_payload = dict(posted)["extract_actions"]
        assert "source_conversation_id" not in memory_payload, (
            "记忆抽取不需要会话 id，多塞字段会让两个 handler 的契约糊在一起"
        )
        assert actions_payload["source_conversation_id"] == conv
        assert actions_payload["source_message_id"]
        assert "助理：" in actions_payload["conversation_text"]

    @pytest.mark.asyncio
    async def test_interrupted_turn_enqueues_nothing(self, monkeypatch):
        """半截建议会变成一条用户根本没读完的待跟进事项。"""
        import app.services.chat_service as svc
        from app.services import interrupt as interrupt_registry

        monkeypatch.setattr(
            svc, "build_provider",
            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("dead")),
        )
        posted = self._spy(monkeypatch)
        monkeypatch.setattr(interrupt_registry, "is_requested", lambda _conv: True)

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            headers = await _auth(client, "act-int")
            conv = (await client.post("/api/v1/conversations",
                                      headers=headers)).json()["id"]
            await client.post(f"/api/v1/conversations/{conv}/messages",
                              headers=headers, json={"text": "我该吃多少"})

        assert posted == []



class TestActionExtractionEmptyOutput:
    """与事实抽取同一个病：空正文当成"没有待办"，待办面板就永远是空的。"""

    @pytest.mark.asyncio
    async def test_empty_output_raises_so_the_job_retries(self, monkeypatch):
        import app.core.actions.extractor as module
        from app.core.llm.client import ChatChunk, LLMError

        class Silent:
            async def stream(self, _request):
                yield ChatChunk(finish_reason="length")

        monkeypatch.setattr(module, "build_provider", lambda: Silent())
        with pytest.raises(LLMError):
            await module.extract_actions("用户：帮我记一下\n助理：我会帮你排一个恢复计划")

    @pytest.mark.asyncio
    async def test_uses_the_configured_output_budget(self, monkeypatch):
        import app.core.actions.extractor as module
        from app.core.config import get_settings
        from app.core.llm.client import ChatChunk

        seen: list[int] = []

        class Recording:
            async def stream(self, request):
                seen.append(request.max_tokens)
                yield ChatChunk(text_delta="[]")

        monkeypatch.setattr(module, "build_provider", lambda: Recording())
        await module.extract_actions("用户：随便说说\n助理：好")
        assert seen == [get_settings().llm_max_tokens]
