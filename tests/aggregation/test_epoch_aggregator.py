"""Юнит-тесты EpochAggregator."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, date, datetime
from unittest.mock import patch

import pytest

from ntrip_accuracy_monitor.application.aggregation import EpochAggregator
from ntrip_accuracy_monitor.domain.epoch import Epoch
from ntrip_accuracy_monitor.domain.position import GeodeticPosition
from ntrip_accuracy_monitor.domain.solution_mode import SolutionMode
from ntrip_accuracy_monitor.protocols.nmea.messages import (
    GgaRecord,
    GsaRecord,
    GstRecord,
    RmcRecord,
    ZdaRecord,
)
from tests.aggregation.conftest import utc


class _EpochCollector:
    def __init__(self) -> None:
        self.epochs: list[Epoch] = []

    async def __call__(self, epoch: Epoch) -> None:
        self.epochs.append(epoch)


@pytest.mark.asyncio
async def test_two_gga_produce_one_epoch_then_open_next(
    make_gga: Callable[..., GgaRecord],
    make_zda: Callable[..., ZdaRecord],
) -> None:
    """GGA n закрывается приходом GGA n+1; n+1 остается открытой."""
    collector = _EpochCollector()
    agg = EpochAggregator("rover_rtk", collector)
    await agg.consume(make_zda(time_utc=utc(hour=12, second=0)))
    await agg.consume(make_gga(time_utc=utc(hour=12, second=0)))
    assert collector.epochs == []
    await agg.consume(make_gga(time_utc=utc(hour=12, second=1)))
    assert len(collector.epochs) == 1
    assert collector.epochs[0].epoch_time == datetime(
        2026, 5, 16, 12, 0, 0, tzinfo=UTC
    )
    await agg.flush_pending()
    assert len(collector.epochs) == 2
    assert collector.epochs[1].epoch_time == datetime(
        2026, 5, 16, 12, 0, 1, tzinfo=UTC
    )


@pytest.mark.asyncio
async def test_gst_associated_with_previous_gga(
    make_gga: Callable[..., GgaRecord],
    make_gst: Callable[..., GstRecord],
    make_zda: Callable[..., ZdaRecord],
) -> None:
    collector = _EpochCollector()
    agg = EpochAggregator("rover_rtk", collector)
    await agg.consume(make_zda(time_utc=utc()))
    await agg.consume(make_gga(time_utc=utc(second=0)))
    await agg.consume(
        make_gst(
            time_utc=utc(second=0),
            sigma_east_m=0.015,
            sigma_north_m=0.012,
            sigma_up_m=0.025,
        )
    )
    await agg.consume(make_gga(time_utc=utc(second=1)))
    e = collector.epochs[0]
    assert e.sigma_east_m == pytest.approx(0.015)
    assert e.sigma_north_m == pytest.approx(0.012)
    assert e.sigma_up_m == pytest.approx(0.025)


@pytest.mark.asyncio
async def test_epoch_without_gst_has_none_sigmas(
    make_gga: Callable[..., GgaRecord],
    make_zda: Callable[..., ZdaRecord],
) -> None:
    collector = _EpochCollector()
    agg = EpochAggregator("rover_rtk", collector)
    await agg.consume(make_zda(time_utc=utc()))
    await agg.consume(make_gga(time_utc=utc(second=0)))
    await agg.consume(make_gga(time_utc=utc(second=1)))
    assert collector.epochs[0].sigma_east_m is None
    assert collector.epochs[0].sigma_north_m is None
    assert collector.epochs[0].sigma_up_m is None


@pytest.mark.asyncio
async def test_gga_without_position_is_dropped(
    make_gga: Callable[..., GgaRecord],
    make_zda: Callable[..., ZdaRecord],
) -> None:
    collector = _EpochCollector()
    agg = EpochAggregator("rover_rtk", collector)
    await agg.consume(make_zda(time_utc=utc()))
    await agg.consume(
        make_gga(
            time_utc=utc(second=0),
            solution_mode=SolutionMode.INVALID,
            position=None,
        )
    )
    await agg.consume(
        make_gga(
            time_utc=utc(second=1),
            solution_mode=SolutionMode.INVALID,
            position=None,
        )
    )
    await agg.consume(make_gga(time_utc=utc(second=2)))
    assert collector.epochs == []
    assert agg.dropped_no_position == 2


@pytest.mark.asyncio
async def test_invalid_mode_with_position_is_written(
    make_gga: Callable[..., GgaRecord],
    make_zda: Callable[..., ZdaRecord],
    default_position: GeodeticPosition,
) -> None:
    """quality=0 (INVALID) с координатами — пишется, переходы режимов ценны."""
    collector = _EpochCollector()
    agg = EpochAggregator("rover_rtk", collector)
    await agg.consume(make_zda(time_utc=utc()))
    await agg.consume(
        make_gga(
            time_utc=utc(second=0),
            solution_mode=SolutionMode.INVALID,
            position=default_position,
            age_of_corrections_s=None,
        )
    )
    await agg.consume(make_gga(time_utc=utc(second=1)))
    assert len(collector.epochs) == 1
    assert collector.epochs[0].solution_mode == SolutionMode.INVALID
    assert agg.dropped_no_position == 0


@pytest.mark.asyncio
async def test_gsa_hdop_overrides_gga_hdop_and_provides_pdop(
    make_gga: Callable[..., GgaRecord],
    make_gsa: Callable[..., GsaRecord],
    make_zda: Callable[..., ZdaRecord],
) -> None:
    collector = _EpochCollector()
    agg = EpochAggregator("rover_rtk", collector)
    await agg.consume(make_zda(time_utc=utc()))
    await agg.consume(make_gga(time_utc=utc(second=0), hdop=1.5))
    await agg.consume(make_gsa(pdop=1.6, hdop=0.6))
    await agg.consume(make_gga(time_utc=utc(second=1)))
    assert collector.epochs[0].hdop == pytest.approx(0.6)
    assert collector.epochs[0].pdop == pytest.approx(1.6)


@pytest.mark.asyncio
async def test_non_positive_dop_values_become_none(
    make_gga: Callable[..., GgaRecord],
    make_zda: Callable[..., ZdaRecord],
) -> None:
    """HDOP=0 — артефакт НАП, в Epoch уйдет None, а не ValueError."""
    collector = _EpochCollector()
    agg = EpochAggregator("rover_rtk", collector)
    await agg.consume(make_zda(time_utc=utc()))
    await agg.consume(make_gga(time_utc=utc(second=0), hdop=0.0))
    await agg.consume(make_gga(time_utc=utc(second=1)))
    assert collector.epochs[0].hdop is None


@pytest.mark.asyncio
async def test_zda_overrides_rmc_overrides_fallback(
    make_rmc: Callable[..., RmcRecord],
    make_zda: Callable[..., ZdaRecord],
) -> None:
    agg = EpochAggregator("rover_rtk", _EpochCollector())
    await agg.consume(
        make_rmc(time_utc=datetime(2025, 1, 1, 12, 0, tzinfo=UTC))
    )
    assert agg.current_date == date(2025, 1, 1)
    assert agg.date_source == "rmc"
    await agg.consume(make_zda(time_utc=utc(year=2026, month=5, day=16)))
    assert agg.current_date == date(2026, 5, 16)
    assert agg.date_source == "zda"
    # Повторный RMC с другой датой — не должен перебивать ZDA.
    await agg.consume(
        make_rmc(time_utc=datetime(2025, 1, 1, 12, 0, tzinfo=UTC))
    )
    assert agg.current_date == date(2026, 5, 16)
    assert agg.date_source == "zda"


@pytest.mark.asyncio
async def test_invalid_rmc_ignored(
    make_rmc: Callable[..., RmcRecord],
) -> None:
    agg = EpochAggregator("rover_rtk", _EpochCollector())
    await agg.consume(
        make_rmc(
            time_utc=datetime(2025, 1, 1, 12, 0, tzinfo=UTC),
            is_valid=False,
        )
    )
    assert agg.current_date is None
    assert agg.date_source == "none"


@pytest.mark.asyncio
async def test_fallback_date_used_when_no_zda_rmc(
    make_gga: Callable[..., GgaRecord],
) -> None:
    collector = _EpochCollector()
    agg = EpochAggregator("rover_rtk", collector)
    fixed_today = datetime(2026, 5, 16, 11, 0, tzinfo=UTC)
    with patch(
        "ntrip_accuracy_monitor.application.aggregation."
        "epoch_aggregator.datetime"
    ) as dt_mock:
        dt_mock.now.return_value = fixed_today
        dt_mock.combine = datetime.combine  # combine оставляем реальный
        await agg.consume(make_gga(time_utc=utc(second=0)))
        await agg.consume(make_gga(time_utc=utc(second=1)))
    assert len(collector.epochs) == 1
    assert collector.epochs[0].epoch_time == datetime(
        2026, 5, 16, 12, 0, 0, tzinfo=UTC
    )
    assert agg.date_source == "fallback"


@pytest.mark.asyncio
async def test_midnight_rollover_advances_date(
    make_gga: Callable[..., GgaRecord],
    make_zda: Callable[..., ZdaRecord],
) -> None:
    collector = _EpochCollector()
    agg = EpochAggregator("rover_rtk", collector)
    await agg.consume(make_zda(time_utc=utc(hour=23, minute=59, second=58)))
    await agg.consume(make_gga(time_utc=utc(hour=23, minute=59, second=58)))
    await agg.consume(make_gga(time_utc=utc(hour=23, minute=59, second=59)))
    await agg.consume(make_gga(time_utc=utc(hour=0, minute=0, second=0)))
    await agg.consume(make_gga(time_utc=utc(hour=0, minute=0, second=1)))
    assert len(collector.epochs) == 3
    assert collector.epochs[0].epoch_time.date() == date(2026, 5, 16)
    assert collector.epochs[1].epoch_time.date() == date(2026, 5, 16)
    assert collector.epochs[2].epoch_time.date() == date(2026, 5, 17)


@pytest.mark.asyncio
async def test_epoch_time_is_timezone_aware_utc(
    make_gga: Callable[..., GgaRecord],
    make_zda: Callable[..., ZdaRecord],
) -> None:
    collector = _EpochCollector()
    agg = EpochAggregator("rover_rtk", collector)
    await agg.consume(make_zda(time_utc=utc()))
    await agg.consume(make_gga(time_utc=utc(second=0)))
    await agg.consume(make_gga(time_utc=utc(second=1)))
    assert collector.epochs[0].epoch_time.tzinfo is UTC


@pytest.mark.asyncio
async def test_stream_id_attached_to_each_epoch(
    make_gga: Callable[..., GgaRecord],
    make_zda: Callable[..., ZdaRecord],
) -> None:
    collector = _EpochCollector()
    agg = EpochAggregator("rover_rtk_42", collector)
    await agg.consume(make_zda(time_utc=utc()))
    await agg.consume(make_gga(time_utc=utc(second=0)))
    await agg.consume(make_gga(time_utc=utc(second=1)))
    assert collector.epochs[0].stream_id == "rover_rtk_42"


@pytest.mark.asyncio
async def test_invalid_age_of_corrections_drops_epoch_safely(
    make_gga: Callable[..., GgaRecord],
    make_zda: Callable[..., ZdaRecord],
) -> None:
    """age > 3600 c — Epoch.__post_init__ бросит ValueError, агрегатор глотает."""
    collector = _EpochCollector()
    agg = EpochAggregator("rover_rtk", collector)
    await agg.consume(make_zda(time_utc=utc()))
    await agg.consume(
        make_gga(time_utc=utc(second=0), age_of_corrections_s=5000.0)
    )
    await agg.consume(make_gga(time_utc=utc(second=1)))
    assert collector.epochs == []
    assert agg.dropped_invalid_format == 1
