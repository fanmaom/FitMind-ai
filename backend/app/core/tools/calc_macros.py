"""计算三大营养素分配。"""

from pydantic import BaseModel, Field

from app.core.domain.macros import Goal, compute_macros
from app.core.tools.registry import ToolContext, tool


class CalcMacrosInput(BaseModel):
    tdee: float = Field(gt=0, description="每日总消耗（kcal），先用 calc_energy_baseline 取得")
    goal: Goal = Field(description="cut=减脂 / bulk=增肌 / maintain=维持")
    weight_kg: float = Field(gt=0, le=500, description="当前体重（kg）")


@tool(
    name="calc_macros",
    description="根据 TDEE 和目标计算每日热量与蛋白质、碳水、脂肪的克数分配。"
                "涉及「该吃多少」的问题必须调用本工具，不要口算。",
    readonly=True,
)
async def calc_macros(inp: CalcMacrosInput, ctx: ToolContext) -> dict:
    r = compute_macros(inp.tdee, inp.goal, inp.weight_kg)
    return {
        "kcal": r.kcal, "protein_g": r.protein_g, "carb_g": r.carb_g,
        "fat_g": r.fat_g, "deficit_kcal": r.deficit_kcal,
    }
