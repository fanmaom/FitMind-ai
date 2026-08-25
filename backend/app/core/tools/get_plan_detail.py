"""按周读取已保存的周期计划。"""

import uuid

from pydantic import BaseModel, Field
from sqlalchemy import select

from app.core.tools.registry import ToolContext, tool
from app.models.plan import Plan


class GetPlanDetailInput(BaseModel):
    plan_id: uuid.UUID = Field(description="plan_strength_cycle 返回的计划 ID")
    week: int = Field(ge=1, le=52, description="要查询的周次，从 1 开始")


@tool(
    name="get_plan_detail",
    description="按 plan_id 查询某一周的计划明细。只返回指定周，避免完整矩阵占满上下文。",
    readonly=True,
)
async def get_plan_detail(inp: GetPlanDetailInput, ctx: ToolContext) -> dict:
    plan = await ctx.session.scalar(
        select(Plan).where(
            Plan.id == inp.plan_id,
            Plan.user_id == ctx.user_id,
            Plan.type == "strength",
        ),
    )
    if plan is None:
        return {"found": False, "note": "计划不存在，或你没有权限读取该计划。"}

    weeks = plan.payload.get("weeks", [])
    detail = next((week for week in weeks if week.get("week") == inp.week), None)
    if detail is None:
        total = len(weeks)
        return {
            "found": False,
            "plan_id": str(plan.id),
            "note": f"该计划只有第 1–{total} 周，请选择有效周次。",
        }
    return {
        "found": True,
        "plan_id": str(plan.id),
        "scheme": plan.payload.get("scheme"),
        **detail,
    }
