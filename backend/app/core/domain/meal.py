"""场景化配餐纯函数。"""

from dataclasses import dataclass
from typing import Literal

from app.core.domain.macros import MacrosResult

Scenario = Literal["cook", "office", "eatout"]


@dataclass(frozen=True)
class MealTarget:
    meal: int
    kcal: float
    protein_g: float
    carb_g: float
    fat_g: float
    is_post_workout: bool = False


@dataclass(frozen=True)
class FoodCandidate:
    name: str
    kcal_per_100g: float
    protein_g: float
    carb_g: float
    fat_g: float
    role: Literal["protein", "carb", "vegetable", "prepared"]
    category: Literal["raw", "ready", "eatout"]


@dataclass(frozen=True)
class MealItem:
    food_name: str
    grams: float
    category: str
    kcal: float


@dataclass(frozen=True)
class MealPlan:
    scenario: Scenario
    target: MealTarget
    items: list[MealItem]
    preparation_note: str


def _split(total: float, weights: list[float]) -> list[float]:
    values = [round(total * weight, 1) for weight in weights[:-1]]
    values.append(round(total - sum(values), 1))
    return values


def distribute_macros(target: MacrosResult, meals: int) -> list[MealTarget]:
    if not 2 <= meals <= 6:
        raise ValueError("meals 必须在 2–6 之间")
    even = [1 / meals] * meals
    carb_weights = list(even)
    # 默认最后一餐为训练后餐，额外倾斜 15% 全天碳水，其余餐均摊扣除。
    shift = 0.15
    carb_weights[-1] += shift
    for index in range(meals - 1):
        carb_weights[index] -= shift / (meals - 1)
    kcals = _split(target.kcal, even)
    proteins = _split(target.protein_g, even)
    carbs = _split(target.carb_g, carb_weights)
    fats = _split(target.fat_g, even)
    return [MealTarget(i + 1, kcals[i], proteins[i], carbs[i], fats[i], i == meals - 1)
            for i in range(meals)]


def build_meal(
    scenario: Scenario, target: MealTarget, foods: list[FoodCandidate], dislikes: list[str],
) -> MealPlan:
    if scenario not in ("cook", "office", "eatout"):
        raise ValueError(f"未知场景：{scenario}")
    allowed_category = {"cook": "raw", "office": "ready", "eatout": "eatout"}[scenario]
    usable = [food for food in foods if food.category == allowed_category and not any(
        dislike and (dislike in food.name or food.name in dislike) for dislike in dislikes
    )]
    if not usable:
        raise ValueError("没有符合场景和忌口条件的食物")

    if scenario == "cook":
        selected = []
        shares = []
        for role, share in (("protein", 0.4), ("carb", 0.4), ("vegetable", 0.2)):
            match = next((food for food in usable if food.role == role), None)
            if match:
                selected.append(match)
                shares.append(share)
        note = "少油烹饪，主食按熟重、肉类按可食部称量；蔬菜可按饱腹感增加。"
    else:
        selected = usable[:2]
        shares = [1 / len(selected)] * len(selected)
        note = "优先选少酱汁、可看清食材的成品；酱料和含糖饮料另计。"

    items = []
    for food, share in zip(selected, shares, strict=True):
        grams = min(500.0, round(target.kcal * share / max(food.kcal_per_100g, 1) * 100, 0))
        items.append(MealItem(food.name, grams, food.category,
                              round(food.kcal_per_100g * grams / 100, 1)))
    return MealPlan(scenario, target, items, note)
