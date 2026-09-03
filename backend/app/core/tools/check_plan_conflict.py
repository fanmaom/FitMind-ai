"""结合用户当前档案检测计划目标冲突。"""

from pydantic import BaseModel, Field

from app.core.domain.conflict import detect_conflict
from app.core.domain.macros import Goal
from app.core.memory.profile import load_profile
from app.core.tools.registry import ToolContext, tool


class CheckPlanConflictInput(BaseModel):
    requested_goal: Goal = Field(description="用户本次请求的目标")
    wants_strength_gain: bool = Field(
        default=False, description="用户是否同时要求提高力量或刷新大重量",
    )


@tool(
    name="check_plan_conflict",
    label="检查目标冲突",
    description="用户同时提出多个目标，或新目标可能与当前减脂/增肌周期冲突时主动调用。自动读取当前周期。",
    readonly=True,
)
async def check_plan_conflict(inp: CheckPlanConflictInput, ctx: ToolContext) -> dict:
    profile = await load_profile(ctx.session, ctx.user_id)
    current_phase = profile.get("goal", inp.requested_goal)
    report = detect_conflict(inp.requested_goal, inp.wants_strength_gain, current_phase)
    # 用 as_dict 而不是 __dict__：前者保证 prompt 字段永远有值（缺失时退回
    # title），前端不必再判空。少一个可空字段就少一处"点了没反应"的可能。
    options = [option.as_dict() for option in report.options]
    result = {
        "has_conflict": report.has_conflict,
        "reason": report.reason,
        "current_phase": current_phase,
        "phase_started_on": profile.get("phase_started_on"),
        "options": options,
    }
    if report.has_conflict:
        result["__card__"] = {"type": "conflict", "payload": result.copy()}
    return result
