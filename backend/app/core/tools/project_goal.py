"""推算到达目标体重的时间。"""

from pydantic import BaseModel, Field

from app.core.domain.projection import project_weight_goal
from app.core.tools.registry import ToolContext, tool


class ProjectGoalInput(BaseModel):
    current_kg: float = Field(gt=0, le=500, description="当前体重（kg）")
    target_kg: float = Field(gt=0, le=500, description="目标体重（kg）")
    weekly_deficit_kcal: float = Field(
        description="每周热量差（kcal）。正数=赤字（减重），负数=盈余（增重）。"
                    "例如每天赤字 500 kcal 则填 3500",
    )


@tool(
    name="project_goal",
    label="推算目标进度",
    description="按每周热量差推算到达目标体重需要多少天，并检查方向是否正确、"
                "速度是否过快。用户问「多久能到」「还要多久」时调用。",
    readonly=True,
)
async def project_goal(inp: ProjectGoalInput, ctx: ToolContext) -> dict:
    r = project_weight_goal(inp.current_kg, inp.target_kg, inp.weekly_deficit_kcal)
    return {
        "days": r.days, "weeks": r.weeks, "weekly_change_kg": r.weekly_change_kg,
        "feasible": r.feasible, "note": r.note,
    }
