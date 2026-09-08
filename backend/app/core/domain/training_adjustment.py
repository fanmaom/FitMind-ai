"""根据计划完成度与 RPE 生成确定性的训练调整建议。"""

from dataclasses import dataclass
from typing import Literal

Action = Literal["increase", "hold", "reduce", "deload", "stop"]


@dataclass(frozen=True)
class TrainingFeedback:
    action: Action
    completion_rate: float
    average_rpe: float | None
    load_multiplier: float
    reason: str


def analyze_training_feedback(
    *, planned_sets: int, planned_reps: int, completed_reps: list[int],
    rpes: list[float], consecutive_failures: int = 0, pain: bool = False,
) -> TrainingFeedback:
    """纯规则分析；不允许模型自行决定训练重量。"""
    if planned_sets <= 0 or planned_reps <= 0:
        raise ValueError("计划组数和次数必须大于 0")
    if any(rep < 0 for rep in completed_reps):
        raise ValueError("完成次数不能小于 0")
    if any(not 1 <= rpe <= 10 for rpe in rpes):
        raise ValueError("RPE 必须在 1–10 之间")

    planned_total = planned_sets * planned_reps
    completed_total = sum(completed_reps[:planned_sets])
    rate = round(min(completed_total / planned_total, 1.0), 3)
    avg_rpe = round(sum(rpes) / len(rpes), 2) if rpes else None

    if pain:
        return TrainingFeedback("stop", rate, avg_rpe, 1.0, "记录中出现疼痛，暂停自动加重并建议专业评估")
    if consecutive_failures >= 2:
        return TrainingFeedback("deload", rate, avg_rpe, 0.925, "连续两次未完成计划，建议提前卸载")
    if rate < 0.8 or (avg_rpe is not None and avg_rpe >= 9.5):
        return TrainingFeedback("reduce", rate, avg_rpe, 0.95, "完成率偏低或主观强度过高")
    if rate >= 0.9 and (avg_rpe is None or avg_rpe <= 8):
        return TrainingFeedback("increase", rate, avg_rpe, 1.02, "完成度良好且仍有余力")
    return TrainingFeedback("hold", rate, avg_rpe, 1.0, "当前表现适合维持重量继续观察")


def round_to_increment(weight: float, increment: float = 2.5) -> float:
    if weight < 0 or increment <= 0:
        raise ValueError("重量不能为负，递增单位必须大于 0")
    return round(round(weight / increment) * increment, 2)
