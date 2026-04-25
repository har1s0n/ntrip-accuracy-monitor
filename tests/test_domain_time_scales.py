from __future__ import annotations
from datetime import UTC, datetime, timedelta, timezone
import pytest

from ntrip_accuracy_monitor.domain.time_scales import (
    CURRENT_GPS_UTC_LEAP_SECONDS,
    GPS_EPOCH,
    ensure_utc,
    gps_to_utc,
    now_utc,
    utc_to_gps,
)


class TestEnsureUtc:
    def test_naive_raises(self) -> None:
        naive = datetime(2026, 4, 24, 12, 0, 0)
        with pytest.raises(ValueError, match="timezone-aware"):
            ensure_utc(naive)

    def test_utc_returned_as_is_same_moment(self) -> None:
        dt = datetime(2026, 4, 24, 12, 0, 0, tzinfo=UTC)
        result = ensure_utc(dt)
        assert result == dt
        assert result.tzinfo is UTC

    def test_other_tz_converts_to_utc(self) -> None:
        msk = timezone(timedelta(hours=3))
        dt_msk = datetime(2026, 4, 24, 15, 0, 0, tzinfo=msk)
        result = ensure_utc(dt_msk)
        assert result == datetime(2026, 4, 24, 12, 0, 0, tzinfo=UTC)
        assert result.utcoffset() == timedelta(0)


class TestNowUtc:
    def test_returns_utc_tz(self) -> None:
        assert now_utc().tzinfo is UTC

    def test_two_calls_monotonic_non_decreasing(self) -> None:
        t1 = now_utc()
        t2 = now_utc()
        assert t2 >= t1


class TestGpsUtcConversion:
    def test_gps_to_utc_at_origin_uses_current_leap_seconds(self) -> None:
        result = gps_to_utc(0.0, 0)
        expected = GPS_EPOCH - timedelta(seconds=CURRENT_GPS_UTC_LEAP_SECONDS)
        assert result == expected
        assert result.tzinfo is UTC

    def test_round_trip_current_era(self) -> None:
        original = datetime(2026, 4, 24, 10, 30, 45, tzinfo=UTC)
        week, sow = utc_to_gps(original)
        restored = gps_to_utc(sow, week)
        assert restored == original

    def test_gps_week_2300_known_moment(self) -> None:
        # GPS week 2300, sow 0 в GPS-шкале = 1980-01-06 + 16100 дней.
        # В UTC это тот же момент минус 18 leap-seconds.
        result = gps_to_utc(0.0, 2300)
        expected_gps_moment = GPS_EPOCH + timedelta(days=2300 * 7)
        expected_utc = expected_gps_moment - timedelta(
            seconds=CURRENT_GPS_UTC_LEAP_SECONDS
        )
        assert result == expected_utc

    def test_utc_to_gps_rejects_naive(self) -> None:
        with pytest.raises(ValueError, match="timezone-aware"):
            utc_to_gps(datetime(2026, 4, 24, 12, 0, 0))

    def test_utc_to_gps_sow_in_range(self) -> None:
        _week, sow = utc_to_gps(datetime(2026, 4, 24, 10, 30, 45, tzinfo=UTC))
        assert 0.0 <= sow < 7 * 86400
