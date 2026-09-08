import pytest

from app.core.domain.training_adjustment import analyze_training_feedback, round_to_increment


@pytest.mark.parametrize(("reps", "rpes", "failures", "pain", "action", "multiplier"), [
    ([5, 5, 5], [7, 8, 8], 0, False, "increase", 1.02),
    ([5, 5, 4], [8.5, 8.5, 9], 0, False, "hold", 1.0),
    ([3, 3, 3], [9, 9, 9], 1, False, "reduce", 0.95),
    ([3, 3, 3], [9, 9, 9], 2, False, "deload", 0.925),
    ([5, 5, 5], [7, 7, 7], 0, True, "stop", 1.0),
])
def test_feedback_rules(reps, rpes, failures, pain, action, multiplier):
    result = analyze_training_feedback(
        planned_sets=3, planned_reps=5, completed_reps=reps, rpes=rpes,
        consecutive_failures=failures, pain=pain,
    )
    assert result.action == action
    assert result.load_multiplier == multiplier


def test_rounds_to_loadable_increment():
    assert round_to_increment(102) == 102.5


def test_rejects_invalid_rpe():
    with pytest.raises(ValueError, match="RPE"):
        analyze_training_feedback(planned_sets=3, planned_reps=5, completed_reps=[5], rpes=[11])
