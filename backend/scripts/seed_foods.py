"""幂等导入中文食物营养数据：python scripts/seed_foods.py。"""

import asyncio
import json
import sys
from pathlib import Path

from sqlalchemy.dialects.postgresql import insert as pg_insert

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.database import async_session_maker
from app.models.food import Food

DATA_FILE = Path(__file__).resolve().parents[1] / "seeds" / "foods.json"


async def seed() -> int:
    payload = json.loads(DATA_FILE.read_text(encoding="utf-8"))
    fields = ["name", "kcal_per_100g", "protein_g", "carb_g", "fat_g", "category", "aliases"]
    foods = [dict(zip(fields, row, strict=True)) if isinstance(row, list) else row
             for row in payload["foods"]]
    async with async_session_maker() as session:
        for item in foods:
            await session.execute(
                pg_insert(Food).values(**item).on_conflict_do_update(
                    index_elements=["name"],
                    set_={key: value for key, value in item.items() if key != "name"},
                ),
            )
        await session.commit()
    return len(foods)


if __name__ == "__main__":
    print(f"已导入 {asyncio.run(seed())} 条食物数据")
