"""L2 事实记忆冲突消解。"""

import pytest
from sqlalchemy import select

from app.core.memory.facts import insert_fact, recall
from app.core.memory.reconcile import reconcile_fact
from app.models.memory import Memory


def _vector(text: str) -> list[float]:
    groups = [
        (0, ("肩", "推举")),
        (1, ("牛肉",)),
        (2, ("香菜",)),
        (3, ("轻食",)),
    ]
    index = 20
    for candidate, words in groups:
        if any(word in text for word in words):
            index = candidate
            break
    result = [0.0] * 1536
    result[index] = 1.0
    return result


@pytest.fixture(autouse=True)
def _stub_models(monkeypatch):
    async def fake_embed(texts):
        return [_vector(text) for text in texts]

    async def fake_judge(old: str, new: str) -> str:
        if old == new:
            return "duplicate"
        if any(word in new for word in ("好了", "康复", "开始吃")):
            return "supersede"
        return "independent"

    monkeypatch.setattr("app.core.memory.facts.embed_texts", fake_embed)
    monkeypatch.setattr("app.core.memory.reconcile.embed_texts", fake_embed)
    monkeypatch.setattr("app.core.memory.reconcile._judge", fake_judge)


class TestContradiction:
    @pytest.mark.asyncio
    async def test_contradicting_fact_supersedes_old(self, db, seeded_user):
        old = await insert_fact(db, seeded_user, "右肩有旧伤，推举时会疼", "injury", 0.9, None)
        await db.commit()
        await reconcile_fact(
            db, seeded_user, "右肩的伤已经好了，可以正常推举", "injury", 0.9, None,
        )
        await db.commit()

        await db.refresh(old)
        assert old.superseded_by is not None, "旧记忆没有被标记失效"

    @pytest.mark.asyncio
    async def test_superseded_not_deleted(self, db, seeded_user):
        old = await insert_fact(db, seeded_user, "不吃牛肉", "preference", 0.9, None)
        await db.commit()
        await reconcile_fact(
            db, seeded_user, "现在开始吃牛肉了", "preference", 0.9, None,
        )
        await db.commit()
        assert await db.get(Memory, old.id) is not None

    @pytest.mark.asyncio
    async def test_only_new_fact_recalled_after_supersede(self, db, seeded_user):
        await insert_fact(db, seeded_user, "右肩有旧伤", "injury", 0.9, None)
        await db.commit()
        await reconcile_fact(db, seeded_user, "右肩已经完全康复", "injury", 0.9, None)
        await db.commit()

        contents = [memory.content for memory in await recall(db, seeded_user, "肩膀能练吗", k=5)]
        assert not any("旧伤" in content for content in contents)


class TestNonConflicting:
    @pytest.mark.asyncio
    async def test_unrelated_fact_is_added(self, db, seeded_user):
        await insert_fact(db, seeded_user, "右肩有旧伤", "injury", 0.9, None)
        await db.commit()
        await reconcile_fact(db, seeded_user, "公司楼下有轻食店", "scenario", 0.9, None)
        await db.commit()

        active = (
            await db.scalars(select(Memory).where(Memory.superseded_by.is_(None)))
        ).all()
        assert len(active) == 2

    @pytest.mark.asyncio
    async def test_duplicate_is_skipped(self, db, seeded_user):
        await insert_fact(db, seeded_user, "不吃香菜", "preference", 0.9, None)
        await db.commit()
        result = await reconcile_fact(
            db, seeded_user, "不吃香菜", "preference", 0.9, None,
        )
        await db.commit()

        active = (
            await db.scalars(select(Memory).where(Memory.superseded_by.is_(None)))
        ).all()
        assert result is None
        assert len(active) == 1


class TestResilience:
    @pytest.mark.asyncio
    async def test_llm_failure_falls_back_to_insert(self, db, seeded_user, monkeypatch):
        import app.core.memory.reconcile as module

        async def boom(*_args, **_kwargs):
            raise RuntimeError("模型不可用")

        monkeypatch.setattr(module, "_judge", boom)
        await insert_fact(db, seeded_user, "右肩有旧伤", "injury", 0.9, None)
        await db.commit()
        result = await reconcile_fact(db, seeded_user, "右肩好了", "injury", 0.9, None)
        await db.commit()

        active = (
            await db.scalars(select(Memory).where(Memory.superseded_by.is_(None)))
        ).all()
        assert result is not None
        assert len(active) == 2

    def test_similarity_threshold_is_stricter_than_recall(self):
        from app.core.memory.facts import MAX_RECALL_DISTANCE
        from app.core.memory.reconcile import SIMILARITY_THRESHOLD

        assert 0 < SIMILARITY_THRESHOLD < MAX_RECALL_DISTANCE
