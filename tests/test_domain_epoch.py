from __future__ import annotations
from datetime import UTC, datetime
import pytest

from ntrip_accuracy_monitor.domain.epoch import Epoch
from ntrip_accuracy_monitor.domain.position import GeodeticPosition
from ntrip_accuracy_monitor.domain.solution_mode import SolutionMode


def _base_position() -> GeodeticPosition:
    return GeodeticPosition(
        latitude_deg=55.7558,
        longitude_deg=37.6173,
        ellipsoidal_height_m=187.5,
    )


def _base_epoch_kwargs() -> dict[str, object]:
    return {
        "epoch_time": datetime(2026, 4, 24, 12, 0, 0, tzinfo=UTC),
        "stream_id": "rover_rtk",
        "position": _base_position(),
        "solution_mode": SolutionMode.RTK_FIXED,
        "age_of_corrections_s": 1.2,
        "satellites_used": 14,
        "hdop": 0.8,
        "pdop": 1.4,
        "sigma_east_m": 0.012,
        "sigma_north_m": 0.011,
        "sigma_up_m": 0.020,
    }


def test_happy_path() -> None:
    epoch = Epoch(**_base_epoch_kwargs())  # type: ignore[arg-type]
    assert epoch.solution_mode is SolutionMode.RTK_FIXED
    assert epoch.epoch_time.tzinfo is UTC
    assert epoch.age_of_corrections_s == 1.2


def test_optional_fields_none_allowed() -> None:
    kwargs = _base_epoch_kwargs() | {
        "solution_mode": SolutionMode.SPP,
        "age_of_corrections_s": None,
        "hdop": None,
        "pdop": None,
        "sigma_east_m": None,
        "sigma_north_m": None,
        "sigma_up_m": None,
    }
    epoch = Epoch(**kwargs)  # type: ignore[arg-type]
    assert epoch.age_of_corrections_s is None


def test_naive_epoch_time_raises() -> None:
    kwargs = _base_epoch_kwargs() | {"epoch_time": datetime(2026, 4, 24, 12, 0, 0)}
    with pytest.raises(ValueError, match="timezone-aware"):
        Epoch(**kwargs)  # type: ignore[arg-type]


def test_empty_stream_id_raises() -> None:
    kwargs = _base_epoch_kwargs() | {"stream_id": ""}
    with pytest.raises(ValueError, match="stream_id"):
        Epoch(**kwargs)  # type: ignore[arg-type]


def test_negative_satellites_raises() -> None:
    kwargs = _base_epoch_kwargs() | {"satellites_used": -1}
    with pytest.raises(ValueError, match="satellites_used"):
        Epoch(**kwargs)  # type: ignore[arg-type]


def test_negative_age_of_corrections_raises() -> None:
    kwargs = _base_epoch_kwargs() | {"age_of_corrections_s": -0.1}
    with pytest.raises(ValueError, match="age_of_corrections_s"):
        Epoch(**kwargs)  # type: ignore[arg-type]


def test_age_of_corrections_exceeds_hour_raises() -> None:
    kwargs = _base_epoch_kwargs() | {"age_of_corrections_s": 3600.01}
    with pytest.raises(ValueError, match="age_of_corrections_s"):
        Epoch(**kwargs)  # type: ignore[arg-type]


def test_age_of_corrections_at_exactly_hour_accepted() -> None:
    kwargs = _base_epoch_kwargs() | {"age_of_corrections_s": 3600.0}
    epoch = Epoch(**kwargs)  # type: ignore[arg-type]
    assert epoch.age_of_corrections_s == 3600.0


@pytest.mark.parametrize("field", ["hdop", "pdop", "sigma_east_m", "sigma_north_m", "sigma_up_m"])
def test_non_positive_dop_or_sigma_raises(field: str) -> None:
    kwargs = _base_epoch_kwargs() | {field: 0.0}
    with pytest.raises(ValueError, match=field):
        Epoch(**kwargs)  # type: ignore[arg-type]


def test_epoch_time_other_tz_normalized_to_utc() -> None:
    from datetime import timedelta, timezone

    msk = timezone(timedelta(hours=3))
    kwargs = _base_epoch_kwargs() | {
        "epoch_time": datetime(2026, 4, 24, 15, 0, 0, tzinfo=msk)
    }
    epoch = Epoch(**kwargs)  # type: ignore[arg-type]
    assert epoch.epoch_time == datetime(2026, 4, 24, 12, 0, 0, tzinfo=UTC)
