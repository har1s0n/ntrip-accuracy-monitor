"""Юнит-тесты compute_age_bin_metrics."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from ntrip_accuracy_monitor.domain.age_bins import (
    AgeBinMetricsSeries,
    compute_age_bin_metrics,
)
from ntrip_accuracy_monitor.domain.epoch import Epoch
from ntrip_accuracy_monitor.domain.metrics import SolutionModeFilter
from ntrip_accuracy_monitor.domain.position import GeodeticPosition
from ntrip_accuracy_monitor.domain.solution_mode import SolutionMode

# Эталон: окрестность Москвы (произвольно).
_REFERENCE = GeodeticPosition(
    latitude_deg=55.7558,
    longitude_deg=37.6173,
    ellipsoidal_height_m=200.0,
)
_T0 = datetime(2025, 6, 1, 12, 0, 0, tzinfo=timezone.utc)

# 1 угловая секунда широты ≈ 30.9 м; шаг в долях угловой секунды даёт
# контролируемое смещение, удобное для проверки выбраковки и метрик.
_DEG_PER_METER_LAT = 1.0 / 111_320.0


def _make_epoch(
    *,
    index: int,
    solution_mode: SolutionMode,
    age_s: float | None,
    delta_north_m: float = 0.0,
    delta_east_m: float = 0.0,
    delta_up_m: float = 0.0,
) -> Epoch:
    """Эпоха со смещением от эталона по NEU в метрах."""
    cos_lat = 0.5621  # cos(55.7558°), достаточно для тестов
    return Epoch(
        epoch_time=_T0 + timedelta(seconds=index),
        stream_id="rover_test",
        position=GeodeticPosition(
            latitude_deg=_REFERENCE.latitude_deg + delta_north_m * _DEG_PER_METER_LAT,
            longitude_deg=_REFERENCE.longitude_deg
                          + delta_east_m * _DEG_PER_METER_LAT / cos_lat,
            ellipsoidal_height_m=_REFERENCE.ellipsoidal_height_m + delta_up_m,
        ),
        solution_mode=solution_mode,
        age_of_corrections_s=age_s,
        satellites_used=12,
        hdop=0.8,
        pdop=1.4,
        sigma_east_m=None,
        sigma_north_m=None,
        sigma_up_m=None,
    )


def test_returns_none_for_spp_filter() -> None:
    """SPP не биннится по Q3 — даже если эпохи есть, ответ None."""
    epochs = [
        _make_epoch(index=i, solution_mode=SolutionMode.SPP, age_s=None)
        for i in range(50)
    ]
    result = compute_age_bin_metrics(
        epochs, _REFERENCE,
        session_id=1, stream_id="rover_test",
        solution_mode_filter=SolutionModeFilter.SPP,
    )
    assert result is None


def test_returns_none_when_no_epochs_match_filter() -> None:
    """Если после фильтра по режиму пусто — None."""
    epochs = [
        _make_epoch(index=i, solution_mode=SolutionMode.SPP, age_s=None)
        for i in range(10)
    ]
    result = compute_age_bin_metrics(
        epochs, _REFERENCE,
        session_id=1, stream_id="rover_test",
        solution_mode_filter=SolutionModeFilter.DGNSS,
    )
    assert result is None


def test_basic_binning_significance_and_counts() -> None:
    """40 DGNSS-эпох в бин [0,1) и 10 в [1,2)."""
    epochs: list[Epoch] = []
    # 40 эпох с age=0.5 — попадут в [0,1), is_significant=True
    epochs += [
        _make_epoch(
            index=i, solution_mode=SolutionMode.DGNSS, age_s=0.5,
            delta_east_m=0.30 + 0.01 * i,
            delta_north_m=-0.20,
            delta_up_m=0.10,
        )
        for i in range(40)
    ]
    # 10 эпох с age=1.5 — попадут в [1,2), is_significant=False (<30)
    epochs += [
        _make_epoch(
            index=40 + i, solution_mode=SolutionMode.DGNSS, age_s=1.5,
            delta_east_m=0.80,
            delta_north_m=-0.50,
            delta_up_m=0.25,
        )
        for i in range(10)
    ]

    result = compute_age_bin_metrics(
        epochs, _REFERENCE,
        session_id=42, stream_id="rover_test",
        solution_mode_filter=SolutionModeFilter.DGNSS,
        min_epochs_per_bin=30,
    )
    assert result is not None
    assert isinstance(result, AgeBinMetricsSeries)
    assert result.epochs_after_filter == 50
    assert result.epochs_with_valid_age == 50  # все age валидны, выбраковка ничего не съест
    assert result.epochs_rejected_outliers == 0
    assert len(result.bins) == 2

    bin_low, bin_high = result.bins  # отсортированы по age_bin_start_s
    assert bin_low.age_bin_start_s == pytest.approx(0.0)
    assert bin_low.age_bin_end_s == pytest.approx(1.0)
    assert bin_low.epochs_count == 40
    assert bin_low.is_significant is True

    assert bin_high.age_bin_start_s == pytest.approx(1.0)
    assert bin_high.age_bin_end_s == pytest.approx(2.0)
    assert bin_high.epochs_count == 10
    assert bin_high.is_significant is False

    # Контракт суммирования (Sigma counts == epochs_with_valid_age).
    assert sum(b.epochs_count for b in result.bins) == result.epochs_with_valid_age


def test_epochs_with_none_age_are_excluded_from_bins() -> None:
    """Эпохи с age=None не попадают в бины, но учтены в epochs_after_filter.

    Отрицательный age проверять через Epoch нельзя — Epoch.__post_init__
    запрещает age_of_corrections_s < 0. Защита от age<0 внутри
    compute_bins_pipeline проверяется отдельным тестом на pipeline-уровне.
    """
    epochs: list[Epoch] = []
    epochs += [
        _make_epoch(
            index=i, solution_mode=SolutionMode.DGNSS, age_s=0.5,
            delta_east_m=0.1, delta_north_m=0.1,
        )
        for i in range(30)
    ]
    # 5 эпох с age=None
    epochs += [
        _make_epoch(
            index=30 + i, solution_mode=SolutionMode.DGNSS, age_s=None,
            delta_east_m=0.1, delta_north_m=0.1,
        )
        for i in range(5)
    ]

    result = compute_age_bin_metrics(
        epochs, _REFERENCE,
        session_id=1, stream_id="rover_test",
        solution_mode_filter=SolutionModeFilter.DGNSS,
        outlier_factor=None,
    )
    assert result is not None
    assert result.epochs_after_filter == 35
    assert result.epochs_rejected_outliers == 0
    assert result.epochs_with_valid_age == 30
    assert len(result.bins) == 1
    assert result.bins[0].epochs_count == 30


def test_outlier_is_rejected_before_binning() -> None:
    """Эпоха-выброс выкидывается до биннинга и не загрязняет бин."""
    # 50 эпох с малой ошибкой (~0.05 м horizontal) и age=0.5
    epochs = [
        _make_epoch(
            index=i, solution_mode=SolutionMode.DGNSS, age_s=0.5,
            delta_east_m=0.03, delta_north_m=0.04,
        )
        for i in range(50)
    ]
    # одна аномалия: 30 м смещение, тот же age. HRMS_first_pass ≈ 4.24 м,
    # порог 5·4.24 ≈ 21.2 м — наш 30-метровый выброс выкидывается.
    epochs.append(
        _make_epoch(
            index=50, solution_mode=SolutionMode.DGNSS, age_s=0.5,
            delta_east_m=20.0, delta_north_m=22.36,
        )
    )

    result = compute_age_bin_metrics(
        epochs, _REFERENCE,
        session_id=1, stream_id="rover_test",
        solution_mode_filter=SolutionModeFilter.DGNSS,
        outlier_factor=5.0,
    )
    assert result is not None
    assert result.epochs_after_filter == 51
    assert result.epochs_rejected_outliers == 1
    assert result.epochs_with_valid_age == 50
    assert len(result.bins) == 1
    # HRMS бина — около 0.05 м, а не «загрязнённое» значение
    assert result.bins[0].hrms_m == pytest.approx(0.05, abs=0.01)


@pytest.mark.parametrize(
    "bin_width_s, min_epochs_per_bin, outlier_factor",
    [
        (0.0, 30, 5.0),
        (-1.0, 30, 5.0),
        (1.0, -1, 5.0),
        (1.0, 30, 0.0),
        (1.0, 30, -2.0),
    ],
)
def test_invalid_parameters_raise(
    bin_width_s: float,
    min_epochs_per_bin: int,
    outlier_factor: float,
) -> None:
    with pytest.raises(ValueError):
        compute_age_bin_metrics(
            [], _REFERENCE,
            session_id=1, stream_id="rover_test",
            solution_mode_filter=SolutionModeFilter.DGNSS,
            bin_width_s=bin_width_s,
            min_epochs_per_bin=min_epochs_per_bin,
            outlier_factor=outlier_factor,
        )
