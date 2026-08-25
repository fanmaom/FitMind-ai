"""体重目标推算。"""

import pytest

from app.core.domain.projection import KCAL_PER_KG_BODYWEIGHT, project_weight_goal


class TestFeasibleProjection:
    def test_projects_weeks_to_target(self):
        # 7kg * 7700 = 53900 kcal；/3500 每周 = 15.4 周
        r = project_weight_goal(current_kg=82, target_kg=75, weekly_deficit_kcal=3500)
        assert r.feasible is True
        assert r.weeks == pytest.approx(15.4, abs=0.1)
        assert r.days == pytest.approx(108, abs=1)

    def test_bulk_direction_works(self):
        r = project_weight_goal(current_kg=70, target_kg=75, weekly_deficit_kcal=-2100)
        assert r.feasible is True
        assert r.weekly_change_kg < 0

    def test_bigger_deficit_means_fewer_weeks(self):
        slow = project_weight_goal(82, 75, 1750)
        fast = project_weight_goal(82, 75, 3500)
        assert fast.weeks < slow.weeks

    def test_conversion_constant_is_used(self):
        r = project_weight_goal(82, 81, KCAL_PER_KG_BODYWEIGHT)
        assert r.weeks == pytest.approx(1.0, abs=0.05)


class TestInfeasible:
    def test_flags_wrong_direction_for_cut(self):
        """想减重却是热量盈余——方向反了。"""
        r = project_weight_goal(current_kg=82, target_kg=75, weekly_deficit_kcal=-2000)
        assert r.feasible is False
        assert "盈余" in r.note

    def test_flags_wrong_direction_for_bulk(self):
        r = project_weight_goal(current_kg=70, target_kg=78, weekly_deficit_kcal=2000)
        assert r.feasible is False
        assert "赤字" in r.note

    def test_already_at_target_is_flagged(self):
        r = project_weight_goal(current_kg=75, target_kg=75, weekly_deficit_kcal=3500)
        assert r.feasible is False


class TestSafety:
    def test_flags_unsafe_rate(self):
        """每周掉秤超过体重 1% 属于过快，容易掉肌肉。"""
        r = project_weight_goal(current_kg=82, target_kg=75, weekly_deficit_kcal=10000)
        assert "过快" in r.note

    def test_safe_rate_has_no_warning(self):
        r = project_weight_goal(current_kg=82, target_kg=75, weekly_deficit_kcal=3500)
        assert r.note == ""


class TestValidation:
    @pytest.mark.parametrize("cur,tgt,deficit", [
        (0, 75, 3500), (-5, 75, 3500), (82, 0, 3500), (82, -75, 3500), (82, 75, 0),
    ])
    def test_rejects_invalid_input(self, cur, tgt, deficit):
        with pytest.raises(ValueError):
            project_weight_goal(cur, tgt, deficit)
