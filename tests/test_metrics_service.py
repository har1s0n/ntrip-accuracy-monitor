"""Интеграционный тест MetricsService.

Использует фикстуру pool из tests/conftest.py. Уборка вручную через
DELETE FROM epochs/sessions — рассчитано на базовый pool без отката
транзакций.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime, timedelta, timezone
from typing import Final

import asyncpg
import pytest

from ntrip_accuracy_monitor.application.service.metrics_service import MetricsService
from ntrip_accuracy_monitor.domain.epoch import Epoch
from ntrip_accuracy_monitor.domain.metrics import SolutionModeFilter
from ntrip_accuracy_monitor.domain.position import GeodeticPosition
from ntrip_accuracy_monitor.domain.solution_mode import SolutionMode
from ntrip_accuracy_monitor.persistence.epoch_repository import EpochRepository
from ntrip_accuracy_monitor.persistence.session_repository import SessionRepository

_REF_LAT: Final[float] = 55.984304296
_REF_LON: Final[float] = 37.213667733
_REF_H: Final[float] = 220.7379
_DEG_PER_METER_LAT: Final[float] = 1.0 / 111_320.0
_T0: Final[datetime] = datetime(2026, 5, 6, 12, 0, 0, tzinfo=timezone.utc)


def _shifted(d_lat_m: float) -> GeodeticPosition:
    """Точка, смещённая на N метров к северу от эталона."""
    return GeodeticPosition(
        latitude_deg=_REF_LAT + d_lat_m * _DEG_PER_METER_LAT,
        longitude_deg=_REF_LON,
        ellipsoidal_height_m=_REF_H,
    )


def _make_epoch(
    *,
    stream_id: str,
    time: datetime,
    solution_mode: SolutionMode,
    d_lat_m: float,
) -> Epoch:
    return Epoch(
        epoch_time=time,
        stream_id=stream_id,
        position=_shifted(d_lat_m),
        solution_mode=solution_mode,
        age_of_corrections_s=None if solution_mode is SolutionMode.SPP else 1.0,
        satellites_used=14,
        hdop=0.8,
        pdop=1.2,
        sigma_east_m=None,
        sigma_north_m=None,
        sigma_up_m=None,
    )


@pytest.fixture
async def session_cleanup(pool: asyncpg.Pool) -> AsyncIterator[list[int]]:
    """Список session_id, которые надо удалить после теста."""
    session_ids: list[int] = []
    yield session_ids
    async with pool.acquire() as conn:
        for sid in session_ids:
            await conn.execute("DELETE FROM epochs WHERE session_id = $1", sid)
            await conn.execute("DELETE FROM sessions WHERE session_id = $1", sid)


def _reference_dict() -> dict[str, float | str]:
    return {
        "latitude_deg": _REF_LAT,
        "longitude_deg": _REF_LON,
        "ellipsoidal_height_m": _REF_H,
        "source": "test",
    }


async def test_compute_session_metrics_returns_one_per_applicable_filter(
    pool: asyncpg.Pool,
    session_cleanup: list[int],
) -> None:
    """Микс RTK_FLOAT → RTK_FIXED → DGNSS → SPP в одном канале даёт
    четыре AccuracyMetrics, по одной на каждый SolutionModeFilter."""
    sessions = SessionRepository(pool)
    epoch_repo = EpochRepository(pool)
    service = MetricsService(sessions, epoch_repo)

    session_id = await sessions.start(
        "metrics service integration",
        reference_antenna=_reference_dict(),
    )
    session_cleanup.append(session_id)

    stream_id = "rover_under_test"
    epochs: list[Epoch] = []
    seconds_from_start = 0

    # 30 эпох RTK_FLOAT с разбросом ±25 см.
    for i in range(30):
        epochs.append(_make_epoch(
            stream_id=stream_id,
            time=_T0 + timedelta(seconds=seconds_from_start),
            solution_mode=SolutionMode.RTK_FLOAT,
            d_lat_m=0.05 * ((i % 10) - 5),
        ))
        seconds_from_start += 1
    # 50 эпох RTK_FIXED с разбросом ±2.5 см.
    for i in range(50):
        epochs.append(_make_epoch(
            stream_id=stream_id,
            time=_T0 + timedelta(seconds=seconds_from_start),
            solution_mode=SolutionMode.RTK_FIXED,
            d_lat_m=0.005 * ((i % 10) - 5),
        ))
        seconds_from_start += 1
    # 20 эпох DGNSS с разбросом ±1.5 м.
    for i in range(20):
        epochs.append(_make_epoch(
            stream_id=stream_id,
            time=_T0 + timedelta(seconds=seconds_from_start),
            solution_mode=SolutionMode.DGNSS,
            d_lat_m=0.3 * ((i % 10) - 5),
        ))
        seconds_from_start += 1
    # 100 эпох SPP с разбросом ±5 м.
    for i in range(100):
        epochs.append(_make_epoch(
            stream_id=stream_id,
            time=_T0 + timedelta(seconds=seconds_from_start),
            solution_mode=SolutionMode.SPP,
            d_lat_m=1.0 * ((i % 10) - 5),
        ))
        seconds_from_start += 1

    await epoch_repo.insert_batch(session_id, epochs)

    results = await service.compute_session_metrics(session_id, stream_id)

    assert len(results) == 4
    by_filter = {m.solution_mode_filter: m for m in results}
    assert set(by_filter) == {
        SolutionModeFilter.RTK_FIXED,
        SolutionModeFilter.RTK_FIXED_FLOAT,
        SolutionModeFilter.DGNSS,
        SolutionModeFilter.SPP,
    }

    # epochs_total — это размер ВСЕЙ выборки канала (200), один и тот же
    # для всех фильтров (интерпретация A, см. чат №10).
    for metric in results:
        assert metric.epochs_total == 200

    # epochs_after_filter — после отбора по solution_mode_filter.
    assert by_filter[SolutionModeFilter.RTK_FIXED].epochs_after_filter == 50
    assert by_filter[SolutionModeFilter.RTK_FIXED_FLOAT].epochs_after_filter == 80
    assert by_filter[SolutionModeFilter.DGNSS].epochs_after_filter == 20
    assert by_filter[SolutionModeFilter.SPP].epochs_after_filter == 100

    # fixed_ratio: 50 fixed / 80 (fixed+float) = 0.625.
    rtk_combo = by_filter[SolutionModeFilter.RTK_FIXED_FLOAT]
    assert rtk_combo.fixed_ratio == pytest.approx(0.625)
    # ttff_s: первая FIXED-эпоха появляется через 30 с после первой FLOAT.
    assert rtk_combo.ttff_s == pytest.approx(30.0)

    # fixed_ratio/ttff_s заполнены ТОЛЬКО для RTK_FIXED_FLOAT.
    for solution_filter in (
            SolutionModeFilter.RTK_FIXED,
            SolutionModeFilter.DGNSS,
            SolutionModeFilter.SPP,
    ):
        assert by_filter[solution_filter].fixed_ratio is None
        assert by_filter[solution_filter].ttff_s is None

    # Содержательная проверка: точность по режимам растёт.
    rtk_fixed = by_filter[SolutionModeFilter.RTK_FIXED]
    dgnss = by_filter[SolutionModeFilter.DGNSS]
    spp = by_filter[SolutionModeFilter.SPP]
    assert rtk_fixed.hrms_m < dgnss.hrms_m < spp.hrms_m


async def test_compute_session_metrics_raises_when_session_not_found(
    pool: asyncpg.Pool,
) -> None:
    """Несуществующий session_id → ValueError."""
    service = MetricsService(SessionRepository(pool), EpochRepository(pool))
    with pytest.raises(ValueError, match="session"):
        await service.compute_session_metrics(
            session_id=2 ** 60, stream_id="any",
        )


async def test_compute_session_metrics_raises_when_no_reference(
    pool: asyncpg.Pool,
    session_cleanup: list[int],
) -> None:
    """Сеанс без reference_antenna → ValueError."""
    sessions = SessionRepository(pool)
    service = MetricsService(sessions, EpochRepository(pool))

    session_id = await sessions.start(
        "test without reference", reference_antenna=None,
    )
    session_cleanup.append(session_id)

    with pytest.raises(ValueError, match="reference"):
        await service.compute_session_metrics(session_id, "any_stream")


async def test_compute_session_metrics_returns_empty_for_stream_without_epochs(
    pool: asyncpg.Pool,
    session_cleanup: list[int],
) -> None:
    """Сеанс есть, эталон есть, эпох в канале нет → пустой список."""
    sessions = SessionRepository(pool)
    service = MetricsService(sessions, EpochRepository(pool))

    session_id = await sessions.start(
        "empty channel test", reference_antenna=_reference_dict(),
    )
    session_cleanup.append(session_id)

    results = await service.compute_session_metrics(session_id, "no_such_stream")
    assert results == []
