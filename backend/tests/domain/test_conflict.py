"""目标冲突检测。"""

from app.core.domain.conflict import detect_conflict


class TestCutVsStrength:
    def test_cut_plus_strength_gain_conflicts(self):
        r = detect_conflict(goal="cut", wants_strength_gain=True, current_phase="cut")
        assert r.has_conflict is True
        assert "赤字" in r.reason
        assert {o.key for o in r.options} == {"cut_then_bulk", "maintain_strength"}

    def test_bulk_plus_strength_gain_is_fine(self):
        r = detect_conflict(goal="bulk", wants_strength_gain=True, current_phase="bulk")
        assert r.has_conflict is False
        assert r.options == []

    def test_cut_without_strength_gain_is_fine(self):
        assert detect_conflict(goal="cut", wants_strength_gain=False,
                               current_phase="cut").has_conflict is False

    def test_maintain_plus_strength_gain_is_not_a_conflict(self):
        """维持期练力量完全可行，不该报冲突。"""
        assert detect_conflict(goal="maintain", wants_strength_gain=True,
                               current_phase="maintain").has_conflict is False


class TestPhaseMismatch:
    def test_goal_contradicts_current_phase(self):
        r = detect_conflict(goal="bulk", wants_strength_gain=False, current_phase="cut")
        assert r.has_conflict is True
        assert "减脂期" in r.reason
        assert {o.key for o in r.options} == {"finish_current", "switch_now"}

    def test_same_phase_no_conflict(self):
        assert detect_conflict(goal="cut", wants_strength_gain=False,
                               current_phase="cut").has_conflict is False

    def test_maintain_goal_never_conflicts_with_phase(self):
        """转维持期是任何时候都合理的选择。"""
        for phase in ("cut", "bulk", "maintain"):
            assert detect_conflict(goal="maintain", wants_strength_gain=False,
                                   current_phase=phase).has_conflict is False

    def test_unknown_phase_is_tolerated(self):
        """档案里没有周期信息时不该报冲突，也不该崩。"""
        assert detect_conflict(goal="bulk", wants_strength_gain=False,
                               current_phase="").has_conflict is False


class TestOptionQuality:
    def test_options_are_actionable_not_vague(self):
        r = detect_conflict(goal="cut", wants_strength_gain=True, current_phase="cut")
        for opt in r.options:
            assert len(opt.summary) > 30, f"方案「{opt.title}」描述太笼统，不可执行"
            assert opt.title

    def test_cut_vs_strength_takes_priority_over_phase_mismatch(self):
        """两种冲突同时成立时，先报生理上更硬的那个。"""
        r = detect_conflict(goal="cut", wants_strength_gain=True, current_phase="bulk")
        assert "赤字" in r.reason
