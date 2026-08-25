"""体重目标推算。纯函数，零依赖。"""

from dataclasses import dataclass

KCAL_PER_KG_BODYWEIGHT = 7700.0     # 约 7700 kcal 对应 1kg 体重
SAFE_WEEKLY_RATE = 0.01             # 每周变化不宜超过体重的 1%


@dataclass(frozen=True)
class GoalProjection:
    days: int
    weeks: float
    weekly_change_kg: float
    feasible: bool
    note: str


def project_weight_goal(
    current_kg: float, target_kg: float, weekly_deficit_kcal: float,
) -> GoalProjection:
    """按每周热量差推算到达目标体重所需时间。

    weekly_deficit_kcal 为正表示每周赤字（减重），为负表示盈余（增重）。
    """
    if current_kg <= 0 or target_kg <= 0:
        raise ValueError(f"体重必须大于 0，收到 current_kg={current_kg} target_kg={target_kg}")
    if weekly_deficit_kcal == 0:
        raise ValueError("weekly_deficit_kcal 不能为 0，否则永远到不了目标")

    diff_kg = current_kg - target_kg          # 正 = 要减重
    weekly_change_kg = weekly_deficit_kcal / KCAL_PER_KG_BODYWEIGHT

    if diff_kg == 0:
        return GoalProjection(
            days=0, weeks=0.0, weekly_change_kg=round(weekly_change_kg, 3), feasible=False,
            note="当前体重已经等于目标体重，无需推算。",
        )

    # 方向一致性：要减重就得赤字，要增重就得盈余
    if diff_kg * weekly_deficit_kcal < 0:
        direction = "赤字" if weekly_deficit_kcal > 0 else "盈余"
        want = "减重" if diff_kg > 0 else "增重"
        return GoalProjection(
            days=0, weeks=0.0, weekly_change_kg=round(weekly_change_kg, 3), feasible=False,
            note=f"目标是{want}，但当前计划为热量{direction}，方向相反，无法达成。",
        )

    weeks = abs(diff_kg) / abs(weekly_change_kg)

    note = ""
    if abs(weekly_change_kg) > current_kg * SAFE_WEEKLY_RATE:
        note = (f"每周变化 {abs(weekly_change_kg):.2f}kg 超过体重的 1%，速度过快，"
                f"容易掉肌肉，建议把周热量差收窄。")

    return GoalProjection(
        days=int(round(weeks * 7)),
        weeks=round(weeks, 1),
        weekly_change_kg=round(weekly_change_kg, 3),
        feasible=True,
        note=note,
    )
