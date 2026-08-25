"""计算基础代谢与每日总消耗。"""

from pydantic import BaseModel, Field

from app.core.domain.energy import ActivityLevel, Sex, calc_bmr, calc_tdee
from app.core.tools.registry import ToolContext, tool


class CalcEnergyBaselineInput(BaseModel):
    weight_kg: float = Field(gt=0, le=500, description="当前体重（kg）")
    height_cm: float = Field(gt=0, le=300, description="身高（cm）")
    age: int = Field(ge=1, le=120, description="年龄（岁）")
    sex: Sex = Field(description="生理性别，影响基础代谢公式：male 或 female")
    activity: ActivityLevel = Field(
        description="日常活动量：sedentary=久坐 / light=每周1-3次 / moderate=每周3-5次 / "
                    "active=每周6-7次 / very_active=体力劳动或一天两练",
    )


@tool(
    name="calc_energy_baseline",
    description="根据身高体重年龄性别与活动量，计算基础代谢率(BMR)与每日总消耗(TDEE)。"
                "任何涉及热量的问题都应先调用本工具拿到基线，不要自行估算。",
    readonly=True,
)
async def calc_energy_baseline(inp: CalcEnergyBaselineInput, ctx: ToolContext) -> dict:
    bmr = calc_bmr(inp.weight_kg, inp.height_cm, inp.age, inp.sex)
    return {"bmr": round(bmr, 1), "tdee": round(calc_tdee(bmr, inp.activity), 1)}
