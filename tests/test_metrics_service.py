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
from ntrip_accuracy_monitor.domain.age_bins import AgeBinMetricsSeries
from ntrip_accuracy_monitor.persistence.age_bin_metrics_repository import (
    AgeBinMetricsRepository,
)
from ntrip_accuracy_monitor.persistence.metrics_repository import (
    MetricsRepository,
)

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


def _make_epoch_with_age(
    *,
    stream_id: str,
    time: datetime,
    solution_mode: SolutionMode,
    age_s: float,
    d_lat_m: float = 0.0,
) -> Epoch:
    """Локальный фабричный метод для тестов биннинга по age.

    Отличается от _make_epoch тем, что age_s — обязательный параметр
    (биннинг — единственное, что нас интересует в этих тестах).
    """
    return Epoch(
        epoch_time=time,
        stream_id=stream_id,
        position=_shifted(d_lat_m),
        solution_mode=solution_mode,
        age_of_corrections_s=age_s,
        satellites_used=14,
        hdop=0.8,
        pdop=1.2,
        sigma_east_m=None,
        sigma_north_m=None,
        sigma_up_m=None,
    )


async def test_compute_session_age_bin_metrics_returns_series_per_applicable_filter(
    pool: asyncpg.Pool,
    session_cleanup: list[int],
) -> None:
    """Смесь DGNSS-эпох с разным age и RTK_FIXED-эпох даёт:
       - серию для DGNSS с четырьмя бинами разной значимости;
       - серию для RTK_FIXED;
       - серию для RTK_FIXED_FLOAT (совпадает с RTK_FIXED — RTK_FLOAT нет);
       - SPP пропускается (даже если бы были, age для SPP всегда None).
    """
    sessions = SessionRepository(pool)
    epoch_repo = EpochRepository(pool)
    service = MetricsService(sessions, epoch_repo)

    session_id = await sessions.start(
        "age binning integration",
        reference_antenna=_reference_dict(),
    )
    session_cleanup.append(session_id)

    stream_id = "rover_under_test"
    epochs: list[Epoch] = []
    seconds_from_start = 0

    # 50 DGNSS с age=0.5 → бин [0,1), is_significant=True
    for i in range(50):
        epochs.append(_make_epoch_with_age(
            stream_id=stream_id,
            time=_T0 + timedelta(seconds=seconds_from_start),
            solution_mode=SolutionMode.DGNSS,
            age_s=0.5,
            d_lat_m=0.05 * ((i % 10) - 5),  # разброс ±0.25 м
        ))
        seconds_from_start += 1
    # 40 DGNSS с age=1.5 → бин [1,2), is_significant=True
    for i in range(40):
        epochs.append(_make_epoch_with_age(
            stream_id=stream_id,
            time=_T0 + timedelta(seconds=seconds_from_start),
            solution_mode=SolutionMode.DGNSS,
            age_s=1.5,
            d_lat_m=0.05 * ((i % 10) - 5),
        ))
        seconds_from_start += 1
    # 20 DGNSS с age=2.5 → бин [2,3), is_significant=False (<30)
    for i in range(20):
        epochs.append(_make_epoch_with_age(
            stream_id=stream_id,
            time=_T0 + timedelta(seconds=seconds_from_start),
            solution_mode=SolutionMode.DGNSS,
            age_s=2.5,
            d_lat_m=0.05 * ((i % 10) - 5),
        ))
        seconds_from_start += 1
    # 10 DGNSS с age=5.5 → бин [5,6), is_significant=False (<30)
    for i in range(10):
        epochs.append(_make_epoch_with_age(
            stream_id=stream_id,
            time=_T0 + timedelta(seconds=seconds_from_start),
            solution_mode=SolutionMode.DGNSS,
            age_s=5.5,
            d_lat_m=0.05 * ((i % 10) - 5),
        ))
        seconds_from_start += 1
    # 30 RTK_FIXED с age=0.5 → отдельная серия с одним бином
    for i in range(30):
        epochs.append(_make_epoch_with_age(
            stream_id=stream_id,
            time=_T0 + timedelta(seconds=seconds_from_start),
            solution_mode=SolutionMode.RTK_FIXED,
            age_s=0.5,
            d_lat_m=0.005 * ((i % 10) - 5),  # разброс ±0.025 м
        ))
        seconds_from_start += 1

    await epoch_repo.insert_batch(session_id, epochs)

    results = await service.compute_session_age_bin_metrics(
        session_id, stream_id,
        bin_width_s=1.0,
        min_epochs_per_bin=30,
    )

    assert len(results) == 3
    for series in results:
        assert isinstance(series, AgeBinMetricsSeries)
    by_filter = {s.solution_mode_filter: s for s in results}
    assert set(by_filter) == {
        SolutionModeFilter.DGNSS,
        SolutionModeFilter.RTK_FIXED,
        SolutionModeFilter.RTK_FIXED_FLOAT,
    }
    assert SolutionModeFilter.SPP not in by_filter

    # DGNSS-серия: 120 эпох, четыре бина.
    dgnss = by_filter[SolutionModeFilter.DGNSS]
    assert dgnss.bin_width_s == pytest.approx(1.0)
    assert dgnss.min_epochs_per_bin == 30
    assert dgnss.epochs_after_filter == 120
    assert dgnss.epochs_rejected_outliers == 0
    assert dgnss.epochs_with_valid_age == 120
    assert len(dgnss.bins) == 4
    # Контракт суммирования: Sigma epochs_count == epochs_with_valid_age
    assert sum(b.epochs_count for b in dgnss.bins) == dgnss.epochs_with_valid_age

    bin_low, bin_mid, bin_high, bin_far = dgnss.bins  # отсортированы по age_bin_start_s
    assert bin_low.age_bin_start_s == pytest.approx(0.0)
    assert bin_low.age_bin_end_s == pytest.approx(1.0)
    assert bin_low.epochs_count == 50
    assert bin_low.is_significant is True

    assert bin_mid.age_bin_start_s == pytest.approx(1.0)
    assert bin_mid.epochs_count == 40
    assert bin_mid.is_significant is True

    assert bin_high.age_bin_start_s == pytest.approx(2.0)
    assert bin_high.epochs_count == 20
    assert bin_high.is_significant is False

    assert bin_far.age_bin_start_s == pytest.approx(5.0)
    assert bin_far.epochs_count == 10
    assert bin_far.is_significant is False

    # RTK_FIXED-серия: один бин с 30 эпохами, значимый.
    rtk_fixed = by_filter[SolutionModeFilter.RTK_FIXED]
    assert rtk_fixed.epochs_after_filter == 30
    assert len(rtk_fixed.bins) == 1
    assert rtk_fixed.bins[0].epochs_count == 30
    assert rtk_fixed.bins[0].is_significant is True

    # RTK_FIXED_FLOAT-серия: совпадает с RTK_FIXED, так как RTK_FLOAT в выборке нет.
    rtk_combo = by_filter[SolutionModeFilter.RTK_FIXED_FLOAT]
    assert rtk_combo.epochs_after_filter == 30
    assert len(rtk_combo.bins) == 1
    assert rtk_combo.bins[0].epochs_count == 30


async def test_compute_session_age_bin_metrics_raises_when_session_not_found(
    pool: asyncpg.Pool,
) -> None:
    """Несуществующий session_id → ValueError."""
    service = MetricsService(SessionRepository(pool), EpochRepository(pool))
    with pytest.raises(ValueError, match="session"):
        await service.compute_session_age_bin_metrics(
            session_id=2 ** 60, stream_id="any",
        )


async def test_compute_session_age_bin_metrics_raises_when_no_reference(
    pool: asyncpg.Pool,
    session_cleanup: list[int],
) -> None:
    """Сеанс без reference_antenna → ValueError (биннинг тоже требует эталона)."""
    sessions = SessionRepository(pool)
    service = MetricsService(sessions, EpochRepository(pool))

    session_id = await sessions.start(
        "age bin no reference", reference_antenna=None,
    )
    session_cleanup.append(session_id)

    with pytest.raises(ValueError, match="reference"):
        await service.compute_session_age_bin_metrics(session_id, "any_stream")


async def test_compute_session_age_bin_metrics_returns_empty_for_stream_without_epochs(
    pool: asyncpg.Pool,
    session_cleanup: list[int],
) -> None:
    """Сеанс есть, эталон есть, эпох в канале нет → пустой список."""
    sessions = SessionRepository(pool)
    service = MetricsService(sessions, EpochRepository(pool))

    session_id = await sessions.start(
        "empty age bin channel", reference_antenna=_reference_dict(),
    )
    session_cleanup.append(session_id)

    results = await service.compute_session_age_bin_metrics(
        session_id, "no_such_stream",
    )
    assert results == []


async def test_compute_session_metrics_persist_writes_to_session_metrics(
    pool: asyncpg.Pool,
    session_cleanup: list[int],
) -> None:
    sessions = SessionRepository(pool)
    epochs_repo = EpochRepository(pool)
    metrics_repo = MetricsRepository(pool)
    age_bin_repo = AgeBinMetricsRepository(pool)
    service = MetricsService(
        sessions, epochs_repo,
        executor=pool,
        metrics_repository=metrics_repo,
        age_bin_metrics_repository=age_bin_repo,
    )

    session_id = await sessions.start(
        "metrics persist test", reference_antenna=_reference_dict(),
    )
    session_cleanup.append(session_id)

    # 10 RTK_FIXED эпох рядом с reference (плавный дрейф ~1 см/эпоху).
    ref = _reference_dict()
    ref_lat: float = ref["latitude_deg"]
    ref_lon: float = ref["longitude_deg"]
    ref_h: float = ref["ellipsoidal_height_m"]
    start = datetime(2026, 5, 26, 12, 0, 0, tzinfo=timezone.utc)

    test_epochs = [
        Epoch(
            epoch_time=start + timedelta(seconds=i),
            stream_id="rover_rtk",
            position=GeodeticPosition(
                latitude_deg=ref_lat + i * 1e-7,
                longitude_deg=ref_lon + i * 1e-7,
                ellipsoidal_height_m=ref_h + i * 0.01,
            ),
            solution_mode=SolutionMode.RTK_FIXED,
            age_of_corrections_s=1.0,
            satellites_used=12,
            hdop=0.8,
            pdop=1.2,
            sigma_east_m=0.01,
            sigma_north_m=0.01,
            sigma_up_m=0.02,
        )
        for i in range(10)
    ]
    await epochs_repo.insert_batch(session_id, test_epochs)

    results = await service.compute_session_metrics(
        session_id, "rover_rtk", persist=True,
    )
    assert len(results) >= 1

    # Round-trip из БД совпадает с тем, что вернул сервис.
    for original in results:
        stored = await metrics_repo.fetch_one(
            session_id, "rover_rtk", original.solution_mode_filter,
        )
        assert stored is not None
        assert stored.hrms_m == pytest.approx(original.hrms_m)
        assert stored.vrms_m == pytest.approx(original.vrms_m)
        assert stored.epochs_total == original.epochs_total
        assert stored.epochs_after_filter == original.epochs_after_filter
        assert stored.computed_at == original.computed_at


async def test_compute_session_age_bin_metrics_persist_writes_bins_and_metrics(
    pool: asyncpg.Pool,
    session_cleanup: list[int],
) -> None:
    sessions = SessionRepository(pool)
    epochs_repo = EpochRepository(pool)
    metrics_repo = MetricsRepository(pool)
    age_bin_repo = AgeBinMetricsRepository(pool)
    service = MetricsService(
        sessions, epochs_repo,
        executor=pool,
        metrics_repository=metrics_repo,
        age_bin_metrics_repository=age_bin_repo,
    )

    session_id = await sessions.start(
        "age bin persist test", reference_antenna=_reference_dict(),
    )
    session_cleanup.append(session_id)

    ref = _reference_dict()
    ref_lat: float = ref["latitude_deg"]
    ref_lon: float = ref["longitude_deg"]
    ref_h: float = ref["ellipsoidal_height_m"]
    start = datetime(2026, 5, 26, 12, 0, 0, tzinfo=timezone.utc)

    # 35 DGNSS-эпох с age=0.5 → попадают в bin [0.0, 1.0), epochs_count=35,
    # is_significant=True (порог по умолчанию 30).
    test_epochs = [
        Epoch(
            epoch_time=start + timedelta(seconds=i),
            stream_id="rover_rtk",
            position=GeodeticPosition(
                latitude_deg=ref_lat + i * 1e-8,
                longitude_deg=ref_lon + i * 1e-8,
                ellipsoidal_height_m=ref_h,
            ),
            solution_mode=SolutionMode.DGNSS,
            age_of_corrections_s=0.5,
            satellites_used=10,
            hdop=1.0,
            pdop=1.5,
            sigma_east_m=0.5,
            sigma_north_m=0.5,
            sigma_up_m=1.0,
        )
        for i in range(35)
    ]
    await epochs_repo.insert_batch(session_id, test_epochs)

    series_list = await service.compute_session_age_bin_metrics(
        session_id, "rover_rtk", persist=True,
    )
    assert len(series_list) >= 1

    # Для каждой series в БД должна быть строка session_metrics с
    # заполненными age_bin_* + соответствующие записи metrics_by_age.
    for series in series_list:
        stored_metrics = await metrics_repo.fetch_one(
            session_id, "rover_rtk", series.solution_mode_filter,
        )
        assert stored_metrics is not None

        meta_row = await pool.fetchrow(
            "SELECT metrics_id, age_bin_width_s, age_bin_min_epochs "
            "FROM session_metrics WHERE session_id=$1 AND stream_id=$2 "
            "AND solution_mode_filter=$3",
            session_id, "rover_rtk", series.solution_mode_filter.value,
        )
        assert meta_row is not None
        assert meta_row["age_bin_width_s"] == pytest.approx(series.bin_width_s)
        assert meta_row["age_bin_min_epochs"] == series.min_epochs_per_bin

        restored = await age_bin_repo.fetch_for_metrics(
            int(meta_row["metrics_id"]),
        )
        assert restored is not None
        assert len(restored.bins) == len(series.bins)
        for orig, got in zip(series.bins, restored.bins, strict=True):
            assert got.age_bin_start_s == pytest.approx(orig.age_bin_start_s)
            assert got.epochs_count == orig.epochs_count
            assert got.is_significant == orig.is_significant
