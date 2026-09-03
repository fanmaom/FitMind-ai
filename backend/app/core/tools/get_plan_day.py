"""查询当前生效计划里某一天的安排。

导入计划的价值不在存档，而在助理能照着它回答"今天吃多少、练什么"。没有这个
工具，导入的 28 天数据就只是一条谁也不会去看的 JSONB。

## 为什么不让模型自己算是哪一天

模型算日期极不可靠——它不知道今天是周几（要靠 prompt 里的日期字符串反推），
更不知道计划是从哪天开始的。这类计算交给纯函数，与项目里"计算不交给模型"的
一致：模型只负责决定"要查哪一天"，日期到周次的换算在这里做。

## 为什么按"计划第几天"而不是日历日期

用户的计划表里没有日期，只有"第 1 周 周一"。硬要绑日历需要一个开始日期，
而那个日期用户多半没想过——他就是想知道"我今天该吃多少"。所以默认按今天是
周几去取当周对应那天，同时允许显式指定周次。
"""

from datetime import date

from pydantic import BaseModel, Field
from sqlalchemy import select

from app.core.tools.registry import ToolContext, tool
from app.models.plan import Plan

_DAY_TYPE_LABELS = {
    "high": "高碳日", "medium": "中碳日", "low": "低碳日", "rest": "休息日",
}

_WEEKDAY_LABELS = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")


class GetPlanDayInput(BaseModel):
    week: int | None = Field(
        default=None, ge=1, le=52,
        description="要查第几周。不填则取第 1 周——用户没说周次时通常是想看当前安排。",
    )
    weekday: int | None = Field(
        default=None, ge=1, le=7,
        description="周几，1 是周一。不填则按今天是周几。",
    )


def _describe(day: dict, week_number: int) -> str:
    """拼一句人话摘要。

    模型拿到结构化数据也会自己组织语言，但给一句现成的摘要能显著降低它
    把数字搞错的概率——尤其是碳水和脂肪在低碳日会反向变化，容易说反。
    """
    weekday = day.get("day", 1)
    label = _WEEKDAY_LABELS[weekday - 1] if 1 <= weekday <= 7 else f"第{weekday}天"
    day_type = _DAY_TYPE_LABELS.get(day.get("day_type", ""), day.get("day_type", ""))

    parts = [f"第{week_number}周{label}"]
    if day_type:
        parts.append(day_type)
    if day.get("focus"):
        parts.append(f"训练：{day['focus']}")

    macros = []
    for key, name, unit in (
        ("kcal", "热量", "kcal"), ("protein_g", "蛋白", "g"),
        ("carb_g", "碳水", "g"), ("fat_g", "脂肪", "g"),
    ):
        value = day.get(key)
        if value is not None:
            macros.append(f"{name} {value:g}{unit}")
    if macros:
        parts.append("、".join(macros))
    return "；".join(parts)


@tool(
    name="get_plan_day",
    label="查询计划当日安排",
    description=(
        "查询用户当前生效计划里某一天的碳水/蛋白/脂肪/热量与训练部位。"
        "用户问「今天吃多少」「今天练什么」「这周三是什么日」时调用。"
        "不填周次和周几就返回今天对应的安排。"
    ),
    readonly=True,
)
async def get_plan_day(inp: GetPlanDayInput, ctx: ToolContext) -> dict:
    # 取最近一份生效的计划。用户手上通常只有一份在跑，按创建时间倒序取第一条
    # 就是"当前那份"；他刚导入的那份自然排在最前。
    plan = await ctx.session.scalar(
        select(Plan)
        .where(Plan.user_id == ctx.user_id, Plan.type == "cut", Plan.status == "active")
        .order_by(Plan.created_at.desc()),
    )
    if plan is None:
        return {
            "found": False,
            "note": "你还没有生效的饮食计划。可以上传一份计划表，或者让我帮你排一个。",
        }

    weeks = (plan.payload or {}).get("weeks", [])
    if not weeks:
        return {"found": False, "note": "这份计划里没有周安排数据。"}

    week_number = inp.week if inp.week is not None else weeks[0].get("week", 1)
    week = next((w for w in weeks if w.get("week") == week_number), None)
    if week is None:
        available = [w.get("week") for w in weeks]
        return {
            "found": False,
            "note": f"这份计划只有第 {min(available)}–{max(available)} 周。",
        }

    # date.isoweekday() 是 1=周一，与计划里的 day 编号一致，不需要换算。
    weekday = inp.weekday if inp.weekday is not None else date.today().isoweekday()
    days = week.get("days", [])
    day = next((d for d in days if d.get("day") == weekday), None)
    if day is None:
        return {
            "found": False,
            "note": f"第{week_number}周里没有{_WEEKDAY_LABELS[weekday - 1]}的安排。",
        }

    return {
        "found": True,
        "plan_id": str(plan.id),
        "week": week_number,
        "weekday": weekday,
        "source": (plan.payload or {}).get("source", "generated"),
        "summary": _describe(day, week_number),
        **{k: v for k, v in day.items() if k != "day"},
    }
