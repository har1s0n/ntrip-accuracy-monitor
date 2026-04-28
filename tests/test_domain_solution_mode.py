from __future__ import annotations

import pytest

from ntrip_accuracy_monitor.domain.solution_mode import SolutionMode


class TestFromGgaQuality:
    @pytest.mark.parametrize(
        "quality,expected",
        [
            (0, SolutionMode.INVALID),
            (1, SolutionMode.SPP),
            (2, SolutionMode.DGNSS),
            (3, SolutionMode.PPS),
            (4, SolutionMode.RTK_FIXED),
            (5, SolutionMode.RTK_FLOAT),
            (6, SolutionMode.DEAD_RECKONING),
            (7, SolutionMode.MANUAL),
            (8, SolutionMode.SIMULATOR),
        ],
    )
    def test_valid_values(self, quality: int, expected: SolutionMode) -> None:
        assert SolutionMode.from_gga_quality(quality) is expected

    @pytest.mark.parametrize("quality", [-1, 9, 100, -100])
    def test_invalid_values_raise(self, quality: int) -> None:
        with pytest.raises(ValueError, match="GGA quality"):
            SolutionMode.from_gga_quality(quality)


class TestProperties:
    def test_is_fixed_solution_only_for_rtk_fixed(self) -> None:
        assert SolutionMode.RTK_FIXED.is_fixed_solution is True
        for other in SolutionMode:
            if other is SolutionMode.RTK_FIXED:
                continue
            assert other.is_fixed_solution is False

    def test_is_differential(self) -> None:
        differential = {
            SolutionMode.DGNSS,
            SolutionMode.RTK_FIXED,
            SolutionMode.RTK_FLOAT,
        }
        for mode in SolutionMode:
            assert mode.is_differential is (mode in differential)

    def test_is_usable(self) -> None:
        assert SolutionMode.INVALID.is_usable is False
        for mode in SolutionMode:
            if mode is SolutionMode.INVALID:
                continue
            assert mode.is_usable is True


def test_str_matches_human_name() -> None:
    assert str(SolutionMode.RTK_FIXED) == SolutionMode.RTK_FIXED.human_name
    assert str(SolutionMode.SPP) == "SPP"
    assert str(SolutionMode.RTK_FIXED) == "RTK Fixed"
