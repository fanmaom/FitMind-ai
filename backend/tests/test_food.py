"""食物库与查询工具。"""

import json
from pathlib import Path

import pytest

from app.core.tools.registry import ToolContext, load_tools, registry
from app.models.food import Food


@pytest.fixture(scope="module", autouse=True)
def _load():
    load_tools()


@pytest.fixture
async def foods(db):
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    rows = [
        {"name": "鸡胸肉", "kcal_per_100g": 120, "protein_g": 24, "carb_g": 0,
         "fat_g": 2, "category": "蛋白", "aliases": ["鸡胸"]},
        {"name": "烤红薯", "kcal_per_100g": 90, "protein_g": 1.2, "carb_g": 21,
         "fat_g": 0.2, "category": "主食", "aliases": ["地瓜", "番薯"]},
    ]
    for row in rows:
        await db.execute(pg_insert(Food).values(**row).on_conflict_do_update(
            index_elements=["name"], set_={k: v for k, v in row.items() if k != "name"},
        ))
    await db.commit()


class TestSearchFood:
    @pytest.mark.asyncio
    async def test_alias_hits_canonical_name(self, db, seeded_user, foods):
        out = await registry.invoke("search_food", {"query": "鸡胸"},
                                    ctx=ToolContext(user_id=seeded_user, session=db))
        assert out["items"][0]["name"] == "鸡胸肉"

    @pytest.mark.asyncio
    async def test_fuzzy_name_match(self, db, seeded_user, foods):
        out = await registry.invoke("search_food", {"query": "红薯"},
                                    ctx=ToolContext(user_id=seeded_user, session=db))
        assert out["found"] is True

    @pytest.mark.asyncio
    async def test_missing_has_clear_note(self, db, seeded_user, foods):
        out = await registry.invoke("search_food", {"query": "火星土豆"},
                                    ctx=ToolContext(user_id=seeded_user, session=db))
        assert out["found"] is False
        assert "没有找到" in out["note"]

    def test_seed_has_demo_scale_and_source(self):
        path = Path(__file__).resolve().parents[1] / "seeds" / "foods.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        assert data["source"]
        assert len(data["foods"]) >= 180
