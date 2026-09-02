"""查询中文食物营养数据。"""

from pydantic import BaseModel, Field
from sqlalchemy import Text, case, cast, or_, select

from app.core.tools.registry import ToolContext, tool
from app.models.food import Food


class SearchFoodInput(BaseModel):
    query: str = Field(min_length=1, max_length=50, description="食物名称或常见别名，如鸡胸、地瓜")
    limit: int = Field(default=5, ge=1, le=10, description="最多返回条数")


@tool(
    name="search_food",
    label="查询食物营养",
    description="查询食物每 100g 的热量和三大营养素。用户询问某种食物营养、热量或配餐选材时调用。",
    readonly=True,
)
async def search_food(inp: SearchFoodInput, ctx: ToolContext) -> dict:
    pattern = f"%{inp.query}%"
    exact_alias = Food.aliases.contains([inp.query])
    rows = (
        await ctx.session.scalars(
            select(Food)
            .where(or_(
                Food.name.ilike(pattern), exact_alias,
                cast(Food.aliases, Text).ilike(pattern),
            ))
            .order_by(case((Food.name == inp.query, 0), (exact_alias, 1), else_=2), Food.name)
            .limit(inp.limit),
        )
    ).all()
    items = [{
        "name": food.name, "kcal_per_100g": food.kcal_per_100g,
        "protein_g": food.protein_g, "carb_g": food.carb_g,
        "fat_g": food.fat_g, "category": food.category,
    } for food in rows]
    return {
        "found": bool(items), "items": items,
        "note": "营养值为常见参考值，品牌和烹饪方式会造成差异。" if items
        else f"没有找到“{inp.query}”，请换用更通用的食物名称。",
    }
