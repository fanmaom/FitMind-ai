"""三大营养素分配。纯函数，零依赖。

策略：先定蛋白（按体重），再定脂肪（按体重，且不低于总热量的 20%），
碳水吃剩下的。极端赤字下碳水会被挤成负数，此时压缩脂肪保住碳水非负。

最后用实际克数反算热量写回 kcal 字段——保证「三大营养素换算回热量 == kcal」
这个不变式成立，否则前端卡片上的数字会自相矛盾。
"""

from dataclasses import dataclass
from typing import Literal

Goal = Literal["cut", "bulk", "maintain"]

KCAL_PER_GRAM: dict[str, int] = {"protein": 4, "carb": 4, "fat": 9}

# 目标 → 相对 TDEE 的热量差
GOAL_DELTA_KCAL: dict[Goal, float] = {"cut": -500.0, "bulk": 300.0, "maintain": 0.0}

# 目标 → 蛋白摄入（g/kg 体重）。减脂期拉高以保住瘦体重。
GOAL_PROTEIN_PER_KG: dict[Goal, float] = {"cut": 2.2, "bulk": 1.8, "maintain": 1.8}

FAT_PER_KG = 0.8
MIN_FAT_KCAL_RATIO = 0.20
MAX_DEFICIT_RATIO = 0.40   # 赤字不超过 TDEE 的 40%，避免给出不安全的极端方案


@dataclass(frozen=True)
class MacrosResult:
    kcal: float
    protein_g: float
    carb_g: float
    fat_g: float
    deficit_kcal: float


def compute_macros(tdee: float, goal: Goal, weight_kg: float) -> MacrosResult:
    """按目标计算每日热量与三大营养素。"""
    if tdee <= 0:
        raise ValueError(f"tdee 必须大于 0，收到 {tdee}")
    if weight_kg <= 0:
        raise ValueError(f"weight_kg 必须大于 0，收到 {weight_kg}")
    if goal not in GOAL_DELTA_KCAL:
        raise ValueError(f"goal 必须是 {sorted(GOAL_DELTA_KCAL)} 之一，收到 {goal}")

    target_kcal = max(tdee + GOAL_DELTA_KCAL[goal], tdee * (1 - MAX_DEFICIT_RATIO))

    protein_g = round(weight_kg * GOAL_PROTEIN_PER_KG[goal], 1)
    protein_kcal = protein_g * KCAL_PER_GRAM["protein"]

    fat_g = round(
        max(weight_kg * FAT_PER_KG, target_kcal * MIN_FAT_KCAL_RATIO / KCAL_PER_GRAM["fat"]), 1,
    )

    remaining = target_kcal - protein_kcal - fat_g * KCAL_PER_GRAM["fat"]
    if remaining < 0:
        # 碳水被挤成负数：把脂肪压到下限，空间让给碳水
        fat_g = round(target_kcal * MIN_FAT_KCAL_RATIO / KCAL_PER_GRAM["fat"], 1)
        remaining = target_kcal - protein_kcal - fat_g * KCAL_PER_GRAM["fat"]
    if remaining < 0:
        # 蛋白本身就吃满了预算（低 TDEE + 大体重）：碳水归零，不再压蛋白
        remaining = 0.0

    carb_g = round(remaining / KCAL_PER_GRAM["carb"], 1)

    actual_kcal = protein_kcal + carb_g * KCAL_PER_GRAM["carb"] + fat_g * KCAL_PER_GRAM["fat"]

    return MacrosResult(
        kcal=round(actual_kcal, 1),
        protein_g=protein_g,
        carb_g=carb_g,
        fat_g=fat_g,
        deficit_kcal=round(actual_kcal - tdee, 1),
    )
