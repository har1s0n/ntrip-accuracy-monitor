"""Тесты MetricsRepository: upsert round-trip, ON CONFLICT, COALESCE,
fetch_one / fetch_for_session.

Все тесты работают в транзакции теста (фикстура db_conn), которая
откатывается по завершению — данные не остаются в БД.
"""

from __future__ import annotations

from datetime import datetime, timezone

import asyncpg
import pytest

from ntrip_accuracy_monitor.domain.metrics import (
    AccuracyMetrics,
    SolutionModeFilter,
)
from ntrip_accuracy_monitor.persistence.metrics_repository import (
    MetricsRepository,
)

pytestmark = pytest.mark.asyncio

_NOW = datetime(2026, 5, 26, 12, 0, 0, tzinfo=timezone.utc)


def _make_metrics(
    session_id: int,
    *,
    stream_id: str = "rover_rtk",
    solution_mode_filter: SolutionModeFilter = SolutionModeFilter.RTK_FIXED,
    hrms_m: float = 0.012,
    epochs_total: int = 100,
    epochs_after_filter: int = 80,
    epochs_rejected_outliers: int = 2,
    fixed_ratio: float | None = None,
    ttff_s: float | None = None,
    computed_at: datetime = _NOW,
) -> AccuracyMetrics:
    """Фабрика AccuracyMetrics с разумными дефолтами."""
    return AccuracyMetrics(
        session_id=session_id,
        stream_id=stream_id,
        solution_mode_filter=solution_mode_filter,
        epochs_total=epochs_total,
        epochs_after_filter=epochs_after_filter,
        epochs_rejected_outliers=epochs_rejected_outliers,
        hrms_m=hrms_m,
        vrms_m=hrms_m * 1.5,
        two_drms_m=hrms_m * 2.0,
        cep50_m=hrms_m * 0.83,
        r95_m=hrms_m * 2.5,
        three_drms_m=hrms_m * 1.8,
        error_3d_mean_m=hrms_m * 1.5,
        error_3d_max_m=hrms_m * 4.0,
        fixed_ratio=fixed_ratio,
        ttff_s=ttff_s,
        skewness_radial=0.1,
        computed_at=computed_at,
    )


async def test_upsert_inserts_new_row_and_returns_id(
    db_conn: asyncpg.Connection,
    sample_session_id: int,
) -> None:
    repo = MetricsRepository(db_conn)
    metrics = _make_metrics(sample_session_id)

    metrics_id = await repo.upsert(metrics, outlier_factor=5.0)

    assert metrics_id > 0
    fetched = await repo.fetch_one(
        sample_session_id, metrics.stream_id, metrics.solution_mode_filter,
    )
    assert fetched is not None
    assert fetched.hrms_m == pytest.approx(metrics.hrms_m)
    assert fetched.epochs_total == metrics.epochs_total
    assert fetched.computed_at == metrics.computed_at


async def test_upsert_on_conflict_keeps_metrics_id_and_updates_fields(
    db_conn: asyncpg.Connection,
    sample_session_id: int,
) -> None:
    """ON CONFLICT DO UPDATE: metrics_id стабилен, поля обновляются (Q1)."""
    repo = MetricsRepository(db_conn)
    first = _make_metrics(sample_session_id, hrms_m=0.020)
    id_first = await repo.upsert(first, outlier_factor=5.0)

    # Тот же ключ, другие значения метрик и параметров.
    second = _make_metrics(
        sample_session_id,
        hrms_m=0.005,
        epochs_total=200,
        epochs_after_filter=180,
        epochs_rejected_outliers=3,
    )
    id_second = await repo.upsert(second, outlier_factor=3.0)

    assert id_second == id_first  # стабильный id — главная гарантия Q1
    fetched = await repo.fetch_one(
        sample_session_id, first.stream_id, first.solution_mode_filter,
    )
    assert fetched is not None
    assert fetched.hrms_m == pytest.approx(0.005)
    assert fetched.epochs_total == 200


async def test_upsert_coalesce_preserves_age_bin_settings(
    db_conn: asyncpg.Connection,
    sample_session_id: int,
) -> None:
    """compute_session_metrics(persist=True) не затирает age_bin_* (Q8)."""
    repo = MetricsRepository(db_conn)
    metrics = _make_metrics(sample_session_id)

    # Первый upsert — с заданными age_bin_*.
    metrics_id = await repo.upsert(
        metrics, outlier_factor=5.0,
        age_bin_width_s=1.0, age_bin_min_epochs=30,
    )
    # Второй upsert — age_bin_*=None (как делает compute_session_metrics).
    same_id = await repo.upsert(
        metrics, outlier_factor=5.0,
        age_bin_width_s=None, age_bin_min_epochs=None,
    )
    assert same_id == metrics_id

    # Проверяем, что age_bin_* НЕ затёрлись.
    row = await db_conn.fetchrow(
        "SELECT age_bin_width_s, age_bin_min_epochs "
        "FROM session_metrics WHERE metrics_id = $1",
        metrics_id,
    )
    assert row is not None
    assert row["age_bin_width_s"] == pytest.approx(1.0)
    assert row["age_bin_min_epochs"] == 30


async def test_fetch_for_session_returns_all_filters_sorted(
    db_conn: asyncpg.Connection,
    sample_session_id: int,
) -> None:
    repo = MetricsRepository(db_conn)
    for filter_ in (
            SolutionModeFilter.RTK_FIXED,
            SolutionModeFilter.DGNSS,
            SolutionModeFilter.SPP,
    ):
        await repo.upsert(
            _make_metrics(sample_session_id, solution_mode_filter=filter_),
            outlier_factor=5.0,
        )

    result = await repo.fetch_for_session(sample_session_id, "rover_rtk")

    assert len(result) == 3
    assert [m.solution_mode_filter for m in result] == [
        SolutionModeFilter.DGNSS,
        SolutionModeFilter.RTK_FIXED,
        SolutionModeFilter.SPP,
    ]  # ORDER BY solution_mode_filter — лексикографически


async def test_fetch_one_returns_none_when_missing(
    db_conn: asyncpg.Connection,
    sample_session_id: int,
) -> None:
    repo = MetricsRepository(db_conn)
    result = await repo.fetch_one(
        sample_session_id, "no_such_stream", SolutionModeFilter.RTK_FIXED,
    )
    assert result is None


async def test_upsert_rejects_invalid_outlier_factor_via_check(
    db_conn: asyncpg.Connection,
    sample_session_id: int,
) -> None:
    """CHECK session_metrics_outlier_factor_check: > 0 либо NULL."""
    repo = MetricsRepository(db_conn)
    metrics = _make_metrics(sample_session_id)
    with pytest.raises(asyncpg.CheckViolationError):
        await repo.upsert(metrics, outlier_factor=0.0)


async def test_upsert_accepts_null_outlier_factor(
    db_conn: asyncpg.Connection,
    sample_session_id: int,
) -> None:
    """outlier_factor=None — выбраковка отключена, должно проходить."""
    repo = MetricsRepository(db_conn)
    metrics = _make_metrics(
        sample_session_id, epochs_rejected_outliers=0,
    )
    metrics_id = await repo.upsert(metrics, outlier_factor=None)
    row = await db_conn.fetchrow(
        "SELECT outlier_factor FROM session_metrics WHERE metrics_id = $1",
        metrics_id,
    )
    assert row is not None
    assert row["outlier_factor"] is None
