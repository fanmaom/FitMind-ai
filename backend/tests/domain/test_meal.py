"""场景化配餐纯函数。"""

import pytest

from app.core.domain.macros import compute_macros
from app.core.domain.meal import FoodCandidate, build_meal, distribute_macros

FOODS = [
    FoodCandidate("鸡胸肉", 120, 24, 0, 2, "protein", "raw"),
    FoodCandidate("牛肉", 125, 21, 0, 4, "protein", "raw"),
    FoodCandidate("米饭", 116, 3, 26, 0, "carb", "raw"),
    FoodCandidate("西兰花", 34, 3, 7, 0, "vegetable", "raw"),
    FoodCandidate("即食鸡胸", 118, 23, 2, 2, "prepared", "ready"),
    FoodCandidate("鸡胸饭", 150, 10, 18, 4, "prepared", "eatout"),
]


def test_distributed_macros_sum_back_and_shift_carbs():
    target = compute_macros(2770, "cut", 82)
    meals = distribute_macros(target, 3)
    assert sum(meal.protein_g for meal in meals) == pytest.approx(target.protein_g, abs=0.1)
    assert sum(meal.kcal for meal in meals) == pytest.approx(target.kcal, abs=0.1)
    assert sum(meal.carb_g for meal in meals) == pytest.approx(target.carb_g, abs=0.1)
    assert meals[-1].carb_g > meals[0].carb_g


def test_respects_dislikes_and_scenarios():
    target = distribute_macros(compute_macros(2500, "cut", 75), 3)[0]
    cooked = build_meal("cook", target, FOODS, ["牛肉"])
    assert all("牛肉" not in item.food_name for item in cooked.items)
    assert cooked.preparation_note
    office = build_meal("office", target, FOODS, [])
    assert all(item.category == "ready" for item in office.items)
