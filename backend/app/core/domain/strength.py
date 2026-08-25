"""1RM 估算与增力周期编排。纯函数，零依赖。"""

from dataclasses import dataclass
from typing import Literal

Scheme = Literal["linear", "5x5", "531"]

PLATE_INCREMENT_KG = 2.5   # 杠铃片最小一侧 1.25kg
DELOAD_EVERY = 4           # 每 4 周一个卸载周
DELOAD_INTENSITY = 0.60
MAX_REPS_FOR_1RM = 30      # Epley 公式在高次数下失准

# 方案 → (起始强度, 组数, 次数)
SCHEME_BASE: dict[Scheme, tuple[float, int, int]] = {
    "linear": (0.75, 3, 5),
    "5x5": (0.80, 5, 5),
    "531": (0.85, 3, 5),
}
LINEAR_WEEKLY_GAIN = 0.02              # 每周 +2% 强度
FIVE_THREE_ONE_REPS = [5, 3, 1]        # 531 三周一轮
FIVE_THREE_ONE_PCT = [0.85, 0.90, 0.95]


@dataclass(frozen=True)
class SetPrescription:
    weight_kg: float
    sets: int
    reps: int
    intensity_pct: float


@dataclass(frozen=True)
class WeekPlan:
    week: int
    is_deload: bool
    lifts: dict[str, SetPrescription]


def _round_to_plate(weight_kg: float) -> float:
    """取整到实际能配出来的重量。出不了这个刻度的重量没法练。"""
    return round(round(weight_kg / PLATE_INCREMENT_KG) * PLATE_INCREMENT_KG, 2)


def estimate_1rm(weight_kg: float, reps: int) -> float:
    """Epley 公式：1RM = w * (1 + reps/30)。"""
    if weight_kg <= 0:
        raise ValueError(f"weight_kg 必须大于 0，收到 {weight_kg}")
    if not 1 <= reps <= MAX_REPS_FOR_1RM:
        raise ValueError(f"reps 必须在 1-{MAX_REPS_FOR_1RM} 之间，收到 {reps}")
    return round(weight_kg * (1 + reps / 30), 2)


def _prescribe(one_rm: float, week: int, scheme: Scheme) -> SetPrescription:
    base_pct, sets, reps = SCHEME_BASE[scheme]

    if week % DELOAD_EVERY == 0:
        pct, sets, reps = DELOAD_INTENSITY, 3, 5
    elif scheme == "531":
        idx = (week - 1) % len(FIVE_THREE_ONE_REPS)
        pct = FIVE_THREE_ONE_PCT[idx]
        reps = FIVE_THREE_ONE_REPS[idx]
    else:
        # linear 与 5x5 都按周递增，只是起点和组次不同
        pct = base_pct + LINEAR_WEEKLY_GAIN * (week - 1)

    # 封顶 100%：线性递增跑久了会超过 1RM，给出不可能完成的重量
    pct = min(pct, 1.0)

    return SetPrescription(
        weight_kg=_round_to_plate(one_rm * pct),
        sets=sets,
        reps=reps,
        intensity_pct=round(pct, 3),
    )


def build_strength_cycle(lifts: dict[str, float], weeks: int, scheme: Scheme) -> list[WeekPlan]:
    """生成周 × 动作的增力计划矩阵。"""
    if not lifts:
        raise ValueError("lifts 不能为空，至少要有一个动作及其当前 1RM")
    for name, one_rm in lifts.items():
        if one_rm <= 0:
            raise ValueError(f"动作「{name}」的 1RM 必须大于 0，收到 {one_rm}")
    if not 1 <= weeks <= 52:
        raise ValueError(f"weeks 必须在 1-52 之间，收到 {weeks}")
    if scheme not in SCHEME_BASE:
        raise ValueError(f"scheme 必须是 {sorted(SCHEME_BASE)} 之一，收到 {scheme}")

    return [
        WeekPlan(
            week=week,
            is_deload=week % DELOAD_EVERY == 0,
            lifts={name: _prescribe(one_rm, week, scheme) for name, one_rm in lifts.items()},
        )
        for week in range(1, weeks + 1)
    ]
