"""经用户确认后，以新版本保存调整后的增力计划。"""

import copy
import uuid

from pydantic import BaseModel, Field
from sqlalchemy import select

from app.core.domain.training_adjustment import round_to_increment
from app.core.tools.analyze_plan_progress import AnalyzePlanProgressInput, build_progress
from app.core.tools.registry import ToolContext, tool
from app.models.plan import Plan


class AdjustStrengthPlanInput(BaseModel):
    plan_id: uuid.UUID | None = Field(default=None, description="计划 ID；未提供时使用最新生效的增力计划")
    week: int = Field(ge=1, le=52)
    exercise: str = Field(min_length=1, max_length=64)
    pain: bool = Field(default=False, description="用户是否报告疼痛")
    confirmed: bool = Field(default=False, description="用户是否明确接受本次调整")


@tool(
    name="adjust_strength_plan", label="调整增力计划",
    description="重新分析完成情况，并在用户明确确认后创建计划新版本；不会覆盖原始计划。",
    readonly=False, needs_confirm=True,
)
async def adjust_strength_plan(inp: AdjustStrengthPlanInput, ctx: ToolContext) -> dict:
    progress_inp = AnalyzePlanProgressInput(
        plan_id=inp.plan_id, week=inp.week, exercise=inp.exercise, pain=inp.pain,
    )
    progress = await build_progress(progress_inp, ctx)
    if not progress.get("found") or not progress.get("can_apply"):
        return progress
    if not inp.confirmed:
        card = {
            **progress, "needs_confirmation": True,
            "confirmation_prompt": (
                f"建议将{inp.exercise}从 {progress['target_weight_kg']}kg 调整为 "
                f"{progress['suggested_weight_kg']}kg。是否接受？"
            ),
        }
        return {**card, "__card__": {"type": "plan_adjustment", "payload": card.copy()}}
    old = await ctx.session.scalar(
        select(Plan).where(
            Plan.id == uuid.UUID(progress["plan_id"]), Plan.user_id == ctx.user_id,
        ).with_for_update(),
    )
    if old is None or old.status != "active":
        return {"found": False, "note": "计划已不是当前版本，请重新分析最新计划。"}
    payload = copy.deepcopy(old.payload)
    target_weight = float(progress["target_weight_kg"])
    if target_weight <= 0:
        return {"found": False, "note": "自重动作暂不支持按百分比自动调整。"}
    for week in payload.get("weeks", []):
        if week.get("week", 0) < inp.week or inp.exercise not in week.get("lifts", {}):
            continue
        lift = week["lifts"][inp.exercise]
        lift["weight_kg"] = round_to_increment(float(lift["weight_kg"]) * (
            progress["suggested_weight_kg"] / target_weight
        ))
    new = Plan(
        user_id=ctx.user_id, type="strength", payload=payload, status="active",
        version=(old.version or 1) + 1, parent_plan_id=old.id,
        adjustment_reason=progress["reason"],
    )
    old.status = "superseded"
    ctx.session.add(new)
    await ctx.session.commit()
    card = {**progress, "old_plan_id": str(old.id), "plan_id": str(new.id), "version": new.version, "applied": True}
    return {**card, "__card__": {"type": "plan_adjustment", "payload": card}}
