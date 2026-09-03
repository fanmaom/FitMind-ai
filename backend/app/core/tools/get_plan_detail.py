"""按周读取已保存的周期计划。"""

import uuid

from pydantic import BaseModel, Field
from sqlalchemy import select

from app.core.tools.registry import ToolContext, tool
from app.models.plan import Plan


class GetPlanDetailInput(BaseModel):
    plan_id: uuid.UUID = Field(description="计划 ID，由排计划或导入计划时返回")
    week: int = Field(ge=1, le=52, description="要查询的周次，从 1 开始")


@tool(
    name="get_plan_detail",
    label="查询计划明细",
    description="按 plan_id 查询某一周的计划明细。只返回指定周，避免完整矩阵占满上下文。",
    readonly=True,
)
async def get_plan_detail(inp: GetPlanDetailInput, ctx: ToolContext) -> dict:
    # 不限制 type。原来这里硬编码了 type=="strength"，于是减脂计划（无论是
    # plan_cut_phase 生成的还是用户导入的）拿着正确的 plan_id 也查不到，
    # 只会得到一句"计划不存在，或你没有权限"——一句会把人引向错误方向的提示。
    #
    # 限定 user_id 就够了：那才是这里真正要防的东西。
    plan = await ctx.session.scalar(
        select(Plan).where(Plan.id == inp.plan_id, Plan.user_id == ctx.user_id),
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
        "plan_type": plan.type,
        "scheme": plan.payload.get("scheme"),
        **detail,
    }
