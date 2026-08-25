"""1RM 估算与增力周期编排。"""

import pytest

from app.core.domain.strength import PLATE_INCREMENT_KG, build_strength_cycle, estimate_1rm


class TestEstimate1RM:
    def test_single_rep_is_itself(self):
        assert estimate_1rm(100, 1) == pytest.approx(103.33, abs=0.01)

    def test_epley_formula(self):
        # 100 * (1 + 5/30) = 116.67
        assert estimate_1rm(100, 5) == pytest.approx(116.67, abs=0.01)

    def test_more_reps_means_higher_1rm(self):
        assert estimate_1rm(100, 8) > estimate_1rm(100, 5) > estimate_1rm(100, 3)

    @pytest.mark.parametrize("weight,reps", [(0, 5), (-10, 5), (100, 0), (100, 31), (100, -1)])
    def test_rejects_invalid_input(self, weight, reps):
        with pytest.raises(ValueError):
            estimate_1rm(weight, reps)


class TestCycleShape:
    LIFTS = {"深蹲": 120.0, "卧推": 90.0}

    def test_returns_one_plan_per_week(self):
        plans = build_strength_cycle(self.LIFTS, weeks=8, scheme="linear")
        assert [p.week for p in plans] == list(range(1, 9))

    def test_every_week_covers_every_lift(self):
        for plan in build_strength_cycle(self.LIFTS, weeks=4, scheme="linear"):
            assert set(plan.lifts) == set(self.LIFTS)

    def test_deload_every_fourth_week(self):
        plans = build_strength_cycle(self.LIFTS, weeks=8, scheme="linear")
        assert [p.week for p in plans if p.is_deload] == [4, 8]

    def test_deload_week_is_lighter(self):
        plans = build_strength_cycle(self.LIFTS, weeks=4, scheme="linear")
        assert plans[3].lifts["深蹲"].weight_kg < plans[2].lifts["深蹲"].weight_kg


class TestSchemes:
    LIFTS = {"深蹲": 120.0}

    def test_linear_progresses_weight(self):
        plans = build_strength_cycle(self.LIFTS, weeks=4, scheme="linear")
        working = [p.lifts["深蹲"].weight_kg for p in plans if not p.is_deload]
        assert working == sorted(working), "线性方案重量必须单调不减"
        assert working[-1] > working[0]

    def test_5x5_prescribes_five_by_five(self):
        plans = build_strength_cycle(self.LIFTS, weeks=4, scheme="5x5")
        working = plans[0].lifts["深蹲"]
        assert (working.sets, working.reps) == (5, 5)

    def test_531_reps_descend_across_weeks(self):
        plans = build_strength_cycle(self.LIFTS, weeks=4, scheme="531")
        assert [p.lifts["深蹲"].reps for p in plans] == [5, 3, 1, 5]

    def test_531_intensity_rises_as_reps_drop(self):
        plans = build_strength_cycle(self.LIFTS, weeks=3, scheme="531")
        pcts = [p.lifts["深蹲"].intensity_pct for p in plans]
        assert pcts == sorted(pcts)


class TestPracticality:
    def test_weight_rounded_to_plate_increment(self):
        """杠铃片最小一侧 1.25kg，即 2.5kg 一档。出不了这个刻度的重量没法练。"""
        plans = build_strength_cycle({"深蹲": 117.3}, weeks=8, scheme="linear")
        for plan in plans:
            w = plan.lifts["深蹲"].weight_kg
            assert round(w / PLATE_INCREMENT_KG, 6) == round(w / PLATE_INCREMENT_KG), \
                f"{w}kg 不是 {PLATE_INCREMENT_KG}kg 的整数倍"

    def test_intensity_pct_within_sane_range(self):
        plans = build_strength_cycle({"深蹲": 120.0}, weeks=12, scheme="linear")
        for plan in plans:
            for pres in plan.lifts.values():
                assert 0.5 <= pres.intensity_pct <= 1.0

    def test_long_cycle_intensity_capped_at_one(self):
        """线性递增跑久了会超过 100% 1RM——必须封顶，否则给出不可能完成的重量。"""
        plans = build_strength_cycle({"深蹲": 120.0}, weeks=40, scheme="linear")
        assert max(p.lifts["深蹲"].intensity_pct for p in plans) <= 1.0


class TestValidation:
    @pytest.mark.parametrize("lifts,weeks,scheme", [
        ({}, 8, "linear"),
        ({"深蹲": 0}, 8, "linear"),
        ({"深蹲": -120}, 8, "linear"),
        ({"深蹲": 120}, 0, "linear"),
        ({"深蹲": 120}, 53, "linear"),
        ({"深蹲": 120}, 8, "unknown"),
    ])
    def test_rejects_invalid_input(self, lifts, weeks, scheme):
        with pytest.raises(ValueError):
            build_strength_cycle(lifts, weeks, scheme)

    def test_error_names_the_offending_lift(self):
        with pytest.raises(ValueError) as exc:
            build_strength_cycle({"深蹲": 120, "卧推": -5}, 8, "linear")
        assert "卧推" in str(exc.value)
