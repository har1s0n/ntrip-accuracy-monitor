"""Тесты AgeBinMetricsRepository: insert_series как replace, fetch_for_metrics,
FK CASCADE, ValueError при отсутствии age_bin_settings."""

from __future__ import annotations

from datetime import datetime, timezone

import asyncpg
import pytest

from ntrip_accuracy_monitor.domain.age_bins import (
    AgeBinMetrics,
    AgeBinMetricsSeries,
)
from ntrip_accuracy_monitor.domain.metrics import (
    AccuracyMetrics,
    SolutionModeFilter,
)
from ntrip_accuracy_monitor.persistence.age_bin_metrics_repository import (
    AgeBinMetricsRepository,
)
from ntrip_accuracy_monitor.persistence.metrics_repository import (
    MetricsRepository,
)

pytestmark = pytest.mark.asyncio

_NOW = datetime(2026, 5, 26, 12, 0, 0, tzinfo=timezone.utc)


def _make_metrics(session_id: int) -> AccuracyMetrics:
    return AccuracyMetrics(
        session_id=session_id,
        stream_id="rover_rtk",
        solution_mode_filter=SolutionModeFilter.RTK_FIXED,
        epochs_total=100, epochs_after_filter=80, epochs_rejected_outliers=2,
        hrms_m=0.012, vrms_m=0.018, two_drms_m=0.024,
        cep50_m=0.010, r95_m=0.030, three_drms_m=0.022,
        error_3d_mean_m=0.018, error_3d_max_m=0.048,
        fixed_ratio=None, ttff_s=None, skewness_radial=0.1,
        computed_at=_NOW,
    )


def _make_series(
    session_id: int,
    *,
    bin_starts: tuple[float, ...] = (0.0, 1.0, 2.0),
    bin_width_s: float = 1.0,
    min_epochs_per_bin: int = 30,
) -> AgeBinMetricsSeries:
    bins = tuple(
        AgeBinMetrics(
            age_bin_start_s=start,
            age_bin_end_s=start + bin_width_s,
            epochs_count=50,
            hrms_m=0.01 + i * 0.005,
            vrms_m=0.02,
            cep50_m=0.008,
            r95_m=0.025,
            is_significant=True,
        )
        for i, start in enumerate(bin_starts)
    )
    return AgeBinMetricsSeries(
        session_id=session_id,
        stream_id="rover_rtk",
        solution_mode_filter=SolutionModeFilter.RTK_FIXED,
        bin_width_s=bin_width_s,
        min_epochs_per_bin=min_epochs_per_bin,
        epochs_after_filter=80,
        epochs_rejected_outliers=2,
        epochs_with_valid_age=sum(b.epochs_count for b in bins),
        bins=bins,
        computed_at=_NOW,
    )


async def test_insert_series_round_trip(
    db_conn: asyncpg.Connection,
    sample_session_id: int,
) -> None:
    metrics_repo = MetricsRepository(db_conn)
    age_repo = AgeBinMetricsRepository(db_conn)

    metrics_id = await metrics_repo.upsert(
        _make_metrics(sample_session_id),
        outlier_factor=5.0,
        age_bin_width_s=1.0,
        age_bin_min_epochs=30,
    )
    series = _make_series(sample_session_id)
    await age_repo.insert_series(series, metrics_id)

    restored = await age_repo.fetch_for_metrics(metrics_id)
    assert restored is not None
    assert restored.session_id == series.session_id
    assert restored.stream_id == series.stream_id
    assert restored.solution_mode_filter == series.solution_mode_filter
    assert restored.bin_width_s == pytest.approx(series.bin_width_s)
    assert restored.min_epochs_per_bin == series.min_epochs_per_bin
    assert len(restored.bins) == len(series.bins)
    for orig, got in zip(series.bins, restored.bins, strict=True):
        assert got.age_bin_start_s == pytest.approx(orig.age_bin_start_s)
        assert got.epochs_count == orig.epochs_count
        assert got.hrms_m == pytest.approx(orig.hrms_m)
        assert got.is_significant == orig.is_significant
    assert restored.epochs_with_valid_age == sum(b.epochs_count for b in series.bins)


async def test_insert_series_replaces_existing_bins(
    db_conn: asyncpg.Connection,
    sample_session_id: int,
) -> None:
    """Повторный вызов с другим набором bins полностью затирает старые (Q4)."""
    metrics_repo = MetricsRepository(db_conn)
    age_repo = AgeBinMetricsRepository(db_conn)

    metrics_id = await metrics_repo.upsert(
        _make_metrics(sample_session_id),
        outlier_factor=5.0,
        age_bin_width_s=1.0, age_bin_min_epochs=30,
    )

    # Первый набор — 3 бина с шагом 1.0.
    await age_repo.insert_series(
        _make_series(sample_session_id, bin_starts=(0.0, 1.0, 2.0)),
        metrics_id,
    )
    count_first = await db_conn.fetchval(
        "SELECT count(*) FROM metrics_by_age WHERE metrics_id = $1",
        metrics_id,
    )
    assert count_first == 3

    # Перерасчёт: bin_width_s=2.0, всего 2 бина (меньше прежних).
    await metrics_repo.upsert(
        _make_metrics(sample_session_id),
        outlier_factor=5.0,
        age_bin_width_s=2.0, age_bin_min_epochs=30,
    )
    await age_repo.insert_series(
        _make_series(
            sample_session_id, bin_starts=(0.0, 2.0), bin_width_s=2.0,
        ),
        metrics_id,
    )
    count_after = await db_conn.fetchval(
        "SELECT count(*) FROM metrics_by_age WHERE metrics_id = $1",
        metrics_id,
    )
    # 2, а не 5 — старые три бина вычищены DELETE-ом внутри insert_series.
    assert count_after == 2


async def test_fetch_for_metrics_returns_none_for_missing_metrics(
    db_conn: asyncpg.Connection,
) -> None:
    age_repo = AgeBinMetricsRepository(db_conn)
    result = await age_repo.fetch_for_metrics(metrics_id=9_999_999)
    assert result is None


async def test_fetch_for_metrics_raises_when_age_bin_settings_null(
    db_conn: asyncpg.Connection,
    sample_session_id: int,
) -> None:
    """Если session_metrics.age_bin_width_s IS NULL — серию не восстановить."""
    metrics_repo = MetricsRepository(db_conn)
    age_repo = AgeBinMetricsRepository(db_conn)

    metrics_id = await metrics_repo.upsert(
        _make_metrics(sample_session_id),
        outlier_factor=5.0,
        # age_bin_*=None — компенсируется COALESCE, но это первая запись:
        # в БД будут NULL.
    )
    with pytest.raises(ValueError, match="age_bin_width_s"):
        await age_repo.fetch_for_metrics(metrics_id)


async def test_cascade_delete_removes_bins(
    db_conn: asyncpg.Connection,
    sample_session_id: int,
) -> None:
    """ON DELETE CASCADE: удаление session_metrics чистит metrics_by_age."""
    metrics_repo = MetricsRepository(db_conn)
    age_repo = AgeBinMetricsRepository(db_conn)

    metrics_id = await metrics_repo.upsert(
        _make_metrics(sample_session_id),
        outlier_factor=5.0,
        age_bin_width_s=1.0, age_bin_min_epochs=30,
    )
    await age_repo.insert_series(_make_series(sample_session_id), metrics_id)

    await db_conn.execute(
        "DELETE FROM session_metrics WHERE metrics_id = $1", metrics_id,
    )
    count = await db_conn.fetchval(
        "SELECT count(*) FROM metrics_by_age WHERE metrics_id = $1",
        metrics_id,
    )
    assert count == 0
