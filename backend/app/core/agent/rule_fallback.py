"""L4 规则兜底：LLM 完全不可用时，用确定性代码回答高频问题。

覆盖不了所有问题，但覆盖到的部分，答案与正常模式逐位一致——
因为走的是同一批 domain 纯函数。

这不是残废模式，是降级但仍可用。它之所以成立，是因为当初把所有计算
放进了纯函数层：那个决定当时是为了避免模型算错，现在顺带给了整个系统
一条不依赖 LLM 的生路。
"""

import re
from dataclasses import dataclass

from app.core.agent.glossary import field_label
from app.core.domain.energy import calc_bmr, calc_tdee
from app.core.domain.macros import compute_macros
from app.core.domain.projection import project_weight_goal

MACRO_PATTERNS = [
    r"吃多少", r"摄入.{0,4}多少", r"多少.{0,4}热量", r"多少.{0,2}[卡大]",
    r"营养素", r"蛋白.{0,4}多少", r"碳水.{0,4}多少", r"该吃",
]
PROJECTION_PATTERNS = [r"多久", r"多长时间", r"什么时候.{0,4}到", r"还要.{0,4}天", r"进度"]

REQUIRED_FOR_MACROS = ("weight_kg", "height_cm", "age", "sex", "activity", "goal")
REQUIRED_FOR_PROJECTION = ("weight_kg", "target_kg")

DEGRADED_PREFIX = "（模型服务暂时不可用，以下由内置公式直接计算，数值准确）\n\n"
INCOMPLETE_HINT = (
    "模型服务暂时不可用，我用内置公式给你算，但你的档案不完整（缺少 {missing}），"
    "补齐后才能给出准确数字。"
)

# 减脂默认每天 500 kcal 赤字；增重默认每天 300 kcal 盈余
DEFAULT_WEEKLY_DEFICIT = 3500.0
DEFAULT_WEEKLY_SURPLUS = -2100.0


@dataclass
class RuleAnswer:
    matched: bool
    text: str = ""
    card: dict | None = None


def _hits(text: str, patterns: list[str]) -> bool:
    return any(re.search(p, text) for p in patterns)


def _missing(profile: dict, required: tuple[str, ...]) -> list[str]:
    """缺哪些字段——返回中文名。

    这句文案是直接给用户看的："缺少 age、sex" 用户根本不知道该补什么，
    而且那是内部字段名，不该出现在回复里。
    """
    return [field_label(k) for k in required if profile.get(k) is None]


def _macro_answer(profile: dict) -> RuleAnswer:
    missing = _missing(profile, REQUIRED_FOR_MACROS)
    if missing:
        return RuleAnswer(matched=True, text=INCOMPLETE_HINT.format(missing="、".join(missing)))

    bmr = calc_bmr(profile["weight_kg"], profile["height_cm"], profile["age"], profile["sex"])
    tdee = calc_tdee(bmr, profile["activity"])
    macros = compute_macros(tdee, profile["goal"], profile["weight_kg"])

    text = (
        f"{DEGRADED_PREFIX}"
        f"基础代谢 {bmr:.0f} kcal，每日总消耗 {tdee:.0f} kcal。\n"
        f"按当前目标，每天摄入 {macros.kcal:.0f} kcal："
        f"蛋白 {macros.protein_g}g、碳水 {macros.carb_g}g、脂肪 {macros.fat_g}g。"
    )
    return RuleAnswer(matched=True, text=text, card={
        "type": "macros",
        "payload": {
            "bmr": round(bmr, 1), "tdee": round(tdee, 1), "kcal": macros.kcal,
            "protein_g": macros.protein_g, "carb_g": macros.carb_g,
            "fat_g": macros.fat_g, "deficit_kcal": macros.deficit_kcal,
        },
    })


def _projection_answer(profile: dict) -> RuleAnswer:
    missing = _missing(profile, REQUIRED_FOR_PROJECTION)
    if missing:
        return RuleAnswer(matched=True, text=INCOMPLETE_HINT.format(missing="、".join(missing)))

    current, target = profile["weight_kg"], profile["target_kg"]
    weekly = DEFAULT_WEEKLY_DEFICIT if current > target else DEFAULT_WEEKLY_SURPLUS
    proj = project_weight_goal(current, target, weekly)

    if not proj.feasible:
        return RuleAnswer(matched=True, text=DEGRADED_PREFIX + proj.note)

    text = (
        f"{DEGRADED_PREFIX}"
        f"从 {current}kg 到 {target}kg，按每周 {abs(proj.weekly_change_kg):.2f}kg 的速度，"
        f"预计 {proj.weeks} 周（{proj.days} 天）。"
    )
    if proj.note:
        text += f"\n\n{proj.note}"
    return RuleAnswer(matched=True, text=text, card={
        "type": "projection",
        "payload": {"days": proj.days, "weeks": proj.weeks,
                    "weekly_change_kg": proj.weekly_change_kg},
    })


def try_rule_fallback(user_text: str, profile: dict) -> RuleAnswer:
    """尝试用规则回答。未命中返回 matched=False，由调用方走 L5 诚实失败。"""
    if _hits(user_text, MACRO_PATTERNS):
        return _macro_answer(profile)
    if _hits(user_text, PROJECTION_PATTERNS):
        return _projection_answer(profile)
    return RuleAnswer(matched=False)
