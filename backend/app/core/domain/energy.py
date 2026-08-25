"""基础代谢与每日总消耗。纯函数，零依赖。

BMR 采用 Mifflin-St Jeor 公式——比 Harris-Benedict 在现代人群上误差更小，
是目前营养学界的主流选择。
"""

from typing import Literal

Sex = Literal["male", "female"]
ActivityLevel = Literal["sedentary", "light", "moderate", "active", "very_active"]

ACTIVITY_FACTORS: dict[ActivityLevel, float] = {
    "sedentary": 1.2,      # 久坐，几乎不运动
    "light": 1.375,        # 每周 1-3 次轻度运动
    "moderate": 1.55,      # 每周 3-5 次中等强度
    "active": 1.725,       # 每周 6-7 次高强度
    "very_active": 1.9,    # 体力劳动或一天两练
}


def calc_bmr(weight_kg: float, height_cm: float, age: int, sex: Sex) -> float:
    """基础代谢率（kcal/天）。

    报错文本会被回灌给模型让它自行纠正参数，所以必须点明字段名和收到的值。
    """
    if weight_kg <= 0:
        raise ValueError(f"weight_kg 必须大于 0，收到 {weight_kg}")
    if height_cm <= 0:
        raise ValueError(f"height_cm 必须大于 0，收到 {height_cm}")
    if not 1 <= age <= 120:
        raise ValueError(f"age 必须在 1-120 之间，收到 {age}")
    if sex not in ("male", "female"):
        raise ValueError(f"sex 必须是 male 或 female，收到 {sex}")

    base = 10 * weight_kg + 6.25 * height_cm - 5 * age
    return base + 5 if sex == "male" else base - 161


def calc_tdee(bmr: float, activity: ActivityLevel) -> float:
    """每日总消耗（kcal/天）。"""
    if bmr <= 0:
        raise ValueError(f"bmr 必须大于 0，收到 {bmr}")
    if activity not in ACTIVITY_FACTORS:
        raise ValueError(f"activity 必须是 {sorted(ACTIVITY_FACTORS)} 之一，收到 {activity}")
    return bmr * ACTIVITY_FACTORS[activity]
