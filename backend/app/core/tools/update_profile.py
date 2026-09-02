"""更新用户档案；敏感推断在确认前只返回征询语。"""

from pydantic import BaseModel, Field

from app.core.agent.glossary import field_label, render_value
from app.core.memory.profile import (
    PROFILE_FIELDS,
    SENSITIVE_FIELDS,
    merge_profile,
    validate_fields,
)
from app.core.tools.registry import ToolContext, tool

_FIELD_DOC = "；".join(f"{key}={description}" for key, description in PROFILE_FIELDS.items())


class UpdateProfileInput(BaseModel):
    updates: dict = Field(description=f"要更新的字段。可用字段：{_FIELD_DOC}")
    confirmed: bool = Field(
        default=False,
        description=(
            "用户是否明确表达过这个信息。用户自己说的填 true；"
            "从对话中推断的填 false，工具会返回征询语"
        ),
    )
    reason: str = Field(default="", description="推断依据；confirmed=false 时用于生成征询语")


@tool(
    name="update_profile",
    label="更新档案",
    description=(
        "更新用户档案（身高体重、目标、伤病、器械、忌口等）。"
        "用户明确告知信息时置 confirmed=true；推断信息置 false，"
        "收到征询语并取得用户确认后再用 true 调用。"
    ),
    readonly=False,
)
async def update_profile(inp: UpdateProfileInput, ctx: ToolContext) -> dict:
    validate_fields(inp.updates)

    touched_sensitive = set(inp.updates) & SENSITIVE_FIELDS
    if touched_sensitive and not inp.confirmed:
        # 这句话是照着念给用户的：字段用中文名（PROFILE_FIELDS 的原文带取值域，
        # 会念出 "当前目标：cut 减脂 / bulk 增肌"），值也翻译成人话。
        described = "、".join(
            f"{field_label(key)}：{render_value(value)}"
            for key, value in sorted(inp.updates.items())
        )
        prompt = (
            f"要我记下{described}吗？"
            f"{'（依据：' + inp.reason + '）' if inp.reason else ''}"
            "这会影响之后所有的计划安排，你确认了我再写进档案。"
        )
        return {
            "written": False,
            "needs_confirmation": True,
            "confirmation_prompt": prompt,
            "pending_fields": sorted(inp.updates),
        }

    merged = await merge_profile(ctx.session, ctx.user_id, inp.updates)
    return {
        "written": True,
        "needs_confirmation": False,
        "updated_fields": sorted(inp.updates),
        "profile": merged,
    }
