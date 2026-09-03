"""计划导入的落库编排。

解析（core/domain/plan_import.py）是纯函数，这一层负责与数据库打交道：
写 plans、按用户意愿合并档案、以及保证重复导入不产生重复计划。

## 为什么分成预览和确认两步

导入会改两样东西：新增一份计划，以及可能覆盖用户档案里的体重、身高、年龄、性别。
第二样是危险的——网上流传的模板通常带着原作者的身体数据（这份样例就是
91kg / 180cm / 25 岁），直接写进去等于把别人的身体参数变成用户自己的，而档案
每轮都注入 prompt，后面所有的热量计算都会按错的体重算。

所以上传只解析、不落库，把"我从文件里读到了什么"完整摆出来让用户看一眼。这也
顺带解决了另一个问题：解析器再怎么容错也可能读错行，预览是唯一能在数据进库前
发现错位的机会。
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.domain.plan_import import ParsedPlan
from app.core.logger import logger
from app.core.memory.profile import PROFILE_FIELDS, merge_profile
from app.models.plan import Plan

# 导入的计划在 plans.type 里的取值。
#
# 沿用 "cut"（与 plan_cut_phase 生成的一致）而不是另起一个 "imported"：
# 计划的**用途**是减脂周期，"谁生成的"是次要维度，靠 payload.source 区分就够。
# 分成两个 type 的代价是每个读计划的地方都要记得查两种，而 get_plan_detail
# 里已经有一处 type=="strength" 的硬编码漏掉了 cut——再加一个 type 只会让
# 这类漏查变多。
IMPORTED_PLAN_TYPE = "cut"

# 从文件里读到的档案字段中，哪些允许写进用户档案。
#
# 只放身体测量值。goal / target_kg 属于 SENSITIVE_FIELDS，是"用户想要什么"
# 而不是"用户是什么"——那得由他自己说，不能由一份下载来的模板决定。
# target_kg 虽然文件里有，也刻意不在这里：模板作者的目标体重与用户无关。
IMPORTABLE_PROFILE_FIELDS = ("weight_kg", "height_cm", "age", "sex")


class PlanImportError(ValueError):
    """落库阶段的失败。消息直接给用户看。"""


@dataclass
class ImportPreview:
    """预览结果。不含任何数据库状态——它在落库之前生成。"""

    week_count: int
    day_count: int
    carb_cycle: bool
    weeks: list[dict]
    basics: dict
    profile_updates: dict
    warnings: list[str]
    fingerprint: str

    def to_response(self) -> dict:
        return {
            "week_count": self.week_count,
            "day_count": self.day_count,
            "carb_cycle": self.carb_cycle,
            "weeks": self.weeks,
            "basics": self.basics,
            # 档案改动单独列出来并带上中文标签，前端才能让用户看清"这次会动
            # 我档案里的哪几项"。混在 basics 里会变成要么全接受要么全不接受。
            "profile_updates": [
                {"field": key, "label": PROFILE_FIELDS.get(key, key), "value": value}
                for key, value in self.profile_updates.items()
            ],
            "warnings": self.warnings,
            "fingerprint": self.fingerprint,
        }


def _fingerprint(payload: dict) -> str:
    """按计划内容算指纹，用于识别重复导入。

    不用文件字节的哈希：同一份计划另存一次、或者改个表格颜色，字节就变了，
    而内容一模一样。用户不会理解"我明明传的是同一份计划，为什么又多了一份"。

    sort_keys 保证同一内容总是得到同一指纹——dict 的遍历顺序在同一进程里稳定，
    但跨进程（server 与 worker）不保证，不排序会让指纹时好时坏。
    """
    canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()[:32]


def build_preview(parsed: ParsedPlan) -> ImportPreview:
    payload = parsed.to_payload()
    updates = {
        key: value
        for key, value in parsed.profile.items()
        if key in IMPORTABLE_PROFILE_FIELDS
    }
    return ImportPreview(
        week_count=len(parsed.weeks),
        day_count=parsed.day_count,
        carb_cycle=bool(payload.get("carb_cycle")),
        weeks=payload["weeks"],
        basics=parsed.basics,
        profile_updates=updates,
        warnings=list(parsed.warnings),
        fingerprint=_fingerprint(payload),
    )


async def find_existing(
    session: AsyncSession, user_id: uuid.UUID, fingerprint: str,
) -> Plan | None:
    """找内容相同且仍在生效的计划。

    只查 active：用户可能有意重新导入一份之前归档掉的计划，那应该允许。
    """
    rows = await session.scalars(
        select(Plan).where(
            Plan.user_id == user_id,
            Plan.type == IMPORTED_PLAN_TYPE,
            Plan.status == "active",
        ),
    )
    for plan in rows:
        if (plan.payload or {}).get("fingerprint") == fingerprint:
            return plan
    return None


async def commit_import(
    session: AsyncSession,
    user_id: uuid.UUID,
    parsed: ParsedPlan,
    *,
    apply_profile: bool = False,
    source_name: str = "",
) -> dict:
    """把解析结果写成一份计划。

    apply_profile 默认 False：档案是用户身份的一部分，默认不动。要改必须显式
    说明——这个默认值的方向比它省下的一次点击重要得多。
    """
    preview = build_preview(parsed)

    existing = await find_existing(session, user_id, preview.fingerprint)
    if existing is not None:
        # 幂等：同一份计划重复导入返回原来那条，不新建。用户点两次上传、或者
        # 网络重试，不该在计划列表里堆出两份一样的东西。
        logger.info(f"计划已存在，跳过导入 plan={existing.id} user={user_id}")
        return {
            "plan_id": str(existing.id),
            "created": False,
            "week_count": preview.week_count,
            "day_count": preview.day_count,
            "profile_applied": {},
            "warnings": preview.warnings,
        }

    payload = parsed.to_payload()
    payload["fingerprint"] = preview.fingerprint
    if source_name:
        # 记下文件名：用户过几周回来看计划列表，"凯圣王碳循环计划_91kg.xlsx"
        # 比一串 UUID 有用得多。
        payload["source_name"] = source_name

    plan = Plan(
        user_id=user_id, type=IMPORTED_PLAN_TYPE, payload=payload, status="active",
    )
    session.add(plan)

    applied: dict = {}
    if apply_profile and preview.profile_updates:
        # merge_profile 自己会 commit，并在体重变化时顺带补一条 body_metrics。
        # 复用它而不是自己写 profile 表——那套白名单校验和体重联动逻辑没有
        # 第二份的理由。
        await merge_profile(session, user_id, dict(preview.profile_updates))
        applied = dict(preview.profile_updates)
    else:
        await session.commit()

    logger.info(
        f"导入计划 plan={plan.id} user={user_id} "
        f"weeks={preview.week_count} days={preview.day_count} profile={bool(applied)}",
    )
    return {
        "plan_id": str(plan.id),
        "created": True,
        "week_count": preview.week_count,
        "day_count": preview.day_count,
        "profile_applied": applied,
        "warnings": preview.warnings,
    }
