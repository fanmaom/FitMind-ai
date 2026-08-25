"""L1 档案层读写。"""

import uuid

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.profile import Profile

# 模型可能编出任意 key。档案会注入每轮 prompt，所以写入必须经过白名单。
# 说明文字也供 prompt 和前端档案面板复用。
PROFILE_FIELDS: dict[str, str] = {
    "height_cm": "身高（cm）",
    "weight_kg": "当前体重（kg）",
    "age": "年龄（岁）",
    "sex": "生理性别：male / female",
    "activity": "日常活动量：sedentary / light / moderate / active / very_active",
    "target_kg": "目标体重（kg）",
    "goal": "当前目标：cut 减脂 / bulk 增肌 / maintain 维持",
    "phase_started_on": "当前周期开始日期（YYYY-MM-DD）",
    "training_split": "训练分化，如「练4休1」「上下肢分化」",
    "training_years": "训练年限",
    "equipment": "可用器械场景，如「健身房」「家里哑铃」",
    "lifts": "各动作当前 1RM，如 {\"深蹲\": 130}",
    "injuries": "伤病列表，排计划时会主动避开",
    "dislikes": "忌口与不吃的食物",
    "meal_scenarios": "各餐场景，如 {\"weekday_lunch\": \"office\"}",
}

# 这些字段会影响整份计划。助理推断出的值必须先由用户确认。
SENSITIVE_FIELDS = {"goal", "target_kg", "injuries", "phase_started_on", "lifts"}


def validate_fields(updates: dict) -> None:
    unknown = [key for key in updates if key not in PROFILE_FIELDS]
    if unknown:
        raise ValueError(
            f"未知的档案字段：{'、'.join(unknown)}。"
            f"可用字段：{'、'.join(sorted(PROFILE_FIELDS))}",
        )


async def load_profile(session: AsyncSession, user_id: uuid.UUID) -> dict:
    row = await session.scalar(select(Profile).where(Profile.user_id == user_id))
    return dict(row.data) if row else {}


async def merge_profile(session: AsyncSession, user_id: uuid.UUID, updates: dict) -> dict:
    """合并写入档案；值为 ``None`` 时清除对应字段。"""
    validate_fields(updates)

    current = await load_profile(session, user_id)
    for key, value in updates.items():
        if value is None:
            current.pop(key, None)
        else:
            current[key] = value

    await session.execute(
        pg_insert(Profile)
        .values(user_id=user_id, data=current)
        .on_conflict_do_update(index_elements=["user_id"], set_={"data": current}),
    )
    await session.commit()
    return current
