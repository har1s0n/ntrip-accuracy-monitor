"""Проверка чистой функции compute_metrics.

Логика тестов: для каждой проверяемой формулы создаём набор Epoch
с известными координатами рядом с эталоном, через уже протестированный
EnuTransformer вычисляем ожидаемое ENU-смещение, далее сверяем
полученное AccuracyMetrics с аналитически рассчитанным значением.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from datetime import datetime, timedelta, timezone

import numpy as np
import pytest

from ntrip_accuracy_monitor.domain.epoch import Epoch
from ntrip_accuracy_monitor.domain.geodetic import EnuTransformer
from ntrip_accuracy_monitor.domain.metrics import (
    AccuracyMetrics,
    SolutionModeFilter,
    compute_metrics,
)
from ntrip_accuracy_monitor.domain.position import GeodeticPosition
from ntrip_accuracy_monitor.domain.solution_mode import SolutionMode

_REFERENCE = GeodeticPosition(
    latitude_deg=55.984304296,
    longitude_deg=37.213667733,
    ellipsoidal_height_m=220.7379,
)
_T0 = datetime(2026, 5, 6, 12, 0, 0, tzinfo=timezone.utc)
_DEG_PER_METER_LAT = 1.0 / 111_320.0
"""Грубое приближение для генерации позиций с заданным N-смещением; для
точных значений сами ENU считаются через EnuTransformer."""


def _make_epoch(
    *,
    position: GeodeticPosition,
    time: datetime,
    solution_mode: SolutionMode = SolutionMode.RTK_FIXED,
) -> Epoch:
    return Epoch(
        epoch_time=time,
        stream_id="rover_rtk",
        position=position,
        solution_mode=solution_mode,
        age_of_corrections_s=1.0,
        satellites_used=14,
        hdop=0.8,
        pdop=1.2,
        sigma_east_m=None,
        sigma_north_m=None,
        sigma_up_m=None,
    )


def _expected_enu_components(
    positions: Sequence[GeodeticPosition],
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    transformer = EnuTransformer.at(_REFERENCE)
    offsets = [transformer.to_enu(p) for p in positions]
    east = np.array([o.east_m for o in offsets], dtype=np.float64)
    north = np.array([o.north_m for o in offsets], dtype=np.float64)
    up = np.array([o.up_m for o in offsets], dtype=np.float64)
    return east, north, up


def _shifted(
    *,
    d_lat_deg: float = 0.0,
    d_lon_deg: float = 0.0,
    d_h_m: float = 0.0,
) -> GeodeticPosition:
    return GeodeticPosition(
        latitude_deg=_REFERENCE.latitude_deg + d_lat_deg,
        longitude_deg=_REFERENCE.longitude_deg + d_lon_deg,
        ellipsoidal_height_m=_REFERENCE.ellipsoidal_height_m + d_h_m,
    )


def test_empty_epochs_returns_none() -> None:
    """Пустая выборка → None."""
    result = compute_metrics(
        [], _REFERENCE,
        session_id=1, stream_id="rover_rtk",
        solution_mode_filter=SolutionModeFilter.RTK_FIXED,
    )
    assert result is None


def test_no_epochs_match_filter_returns_none() -> None:
    """Эпохи есть, но ни одна не соответствует фильтру → None."""
    epochs = [
        _make_epoch(position=_REFERENCE, time=_T0, solution_mode=SolutionMode.SPP)
    ]
    result = compute_metrics(
        epochs, _REFERENCE,
        session_id=1, stream_id="rover_rtk",
        solution_mode_filter=SolutionModeFilter.RTK_FIXED,
    )
    assert result is None


def test_formulas_match_methodology() -> None:
    """HRMS, VRMS, 2DRMS, CEP50, R95, 3D-RMS — против эталона из NumPy."""
    positions = [
        _shifted(d_lat_deg=+1e-5, d_h_m=+0.10),
        _shifted(d_lat_deg=-1e-5, d_h_m=-0.05),
        _shifted(d_lon_deg=+1e-5, d_h_m=+0.20),
        _shifted(d_lon_deg=-1e-5, d_h_m=-0.10),
        _shifted(d_lat_deg=+5e-6, d_lon_deg=+5e-6, d_h_m=+0.15),
    ]
    east, north, up = _expected_enu_components(positions)
    horizontal_sq = east ** 2 + north ** 2
    horizontal = np.sqrt(horizontal_sq)
    total_3d = np.sqrt(horizontal_sq + up ** 2)

    expected_hrms = math.sqrt(float(np.mean(horizontal_sq)))
    expected_vrms = math.sqrt(float(np.mean(up ** 2)))
    expected_cep50 = float(np.median(horizontal))
    expected_r95 = float(np.percentile(horizontal, 95.0, method="hazen"))
    expected_3drms = math.sqrt(float(np.mean(horizontal_sq + up ** 2)))
    expected_mean_3d = float(np.mean(total_3d))
    expected_max_3d = float(np.max(total_3d))

    epochs = [
        _make_epoch(position=p, time=_T0 + timedelta(seconds=i))
        for i, p in enumerate(positions)
    ]
    metrics = compute_metrics(
        epochs, _REFERENCE,
        session_id=42, stream_id="rover_rtk",
        solution_mode_filter=SolutionModeFilter.RTK_FIXED,
        outlier_factor=None,  # выбраковка выключена для чистой проверки формул
    )

    assert metrics is not None
    assert metrics.epochs_total == 5
    assert metrics.epochs_after_filter == 5
    assert metrics.epochs_rejected_outliers == 0
    assert metrics.hrms_m == pytest.approx(expected_hrms, rel=1e-10)
    assert metrics.vrms_m == pytest.approx(expected_vrms, rel=1e-10)
    assert metrics.two_drms_m == pytest.approx(2.0 * expected_hrms, rel=1e-10)
    assert metrics.cep50_m == pytest.approx(expected_cep50, rel=1e-10)
    assert metrics.r95_m == pytest.approx(expected_r95, rel=1e-10)
    assert metrics.three_drms_m == pytest.approx(expected_3drms, rel=1e-10)
    assert metrics.error_3d_mean_m == pytest.approx(expected_mean_3d, rel=1e-10)
    assert metrics.error_3d_max_m == pytest.approx(expected_max_3d, rel=1e-10)
    assert metrics.fixed_ratio is None
    assert metrics.ttff_s is None


def test_two_pass_outlier_rejection_removes_only_extreme_epochs() -> None:
    # 100 эпох с горизонтальной ошибкой до ±10 см (равномерно по широте).
    horizontal_offsets_m = np.linspace(-0.10, 0.10, 100)
    base_positions = [
        _shifted(d_lat_deg=offset_m * _DEG_PER_METER_LAT)
        for offset_m in horizontal_offsets_m
    ]
    # Один резкий выброс: 10 м по широте — заведомо за пределами 5·HRMS_first.
    outlier = _shifted(d_lat_deg=10.0 * _DEG_PER_METER_LAT)
    positions = [*base_positions, outlier]
    epochs = [
        _make_epoch(position=p, time=_T0 + timedelta(seconds=i))
        for i, p in enumerate(positions)
    ]

    metrics = compute_metrics(
        epochs, _REFERENCE,
        session_id=1, stream_id="rover_rtk",
        solution_mode_filter=SolutionModeFilter.RTK_FIXED,
        outlier_factor=5.0,
    )

    assert metrics is not None
    assert metrics.epochs_after_filter == 101
    assert metrics.epochs_rejected_outliers == 1
    # HRMS после выбраковки — единицы см, не метров.
    assert metrics.hrms_m < 0.10


def test_outlier_factor_none_keeps_all_epochs() -> None:
    """outlier_factor=None → выбраковка выключена, все эпохи в формулах."""
    positions = [_shifted(d_lat_deg=i * 1e-6) for i in range(5)]
    epochs = [
        _make_epoch(position=p, time=_T0 + timedelta(seconds=i))
        for i, p in enumerate(positions)
    ]
    metrics = compute_metrics(
        epochs, _REFERENCE,
        session_id=1, stream_id="rover_rtk",
        solution_mode_filter=SolutionModeFilter.RTK_FIXED,
        outlier_factor=None,
    )
    assert metrics is not None
    assert metrics.epochs_rejected_outliers == 0


def test_invalid_outlier_factor_raises() -> None:
    """outlier_factor <= 0 — ValueError."""
    epochs = [_make_epoch(position=_REFERENCE, time=_T0)]
    with pytest.raises(ValueError, match="outlier_factor"):
        compute_metrics(
            epochs, _REFERENCE,
            session_id=1, stream_id="rover_rtk",
            solution_mode_filter=SolutionModeFilter.RTK_FIXED,
            outlier_factor=0.0,
        )


def test_rtk_fixed_float_computes_ratio_and_ttff() -> None:
    """RTK_FIXED_FLOAT: fixed_ratio = N_fixed / (N_fixed + N_float);
    ttff_s = время от первой эпохи до первой FIXED."""
    positions = [_shifted(d_lat_deg=i * 1e-7) for i in range(10)]
    # Первые 3 эпохи — FLOAT, остальные 7 — FIXED.
    modes: list[SolutionMode] = (
        [SolutionMode.RTK_FLOAT] * 3 + [SolutionMode.RTK_FIXED] * 7
    )
    epochs = [
        _make_epoch(position=p, time=_T0 + timedelta(seconds=i), solution_mode=m)
        for i, (p, m) in enumerate(zip(positions, modes, strict=True))
    ]
    metrics = compute_metrics(
        epochs, _REFERENCE,
        session_id=1, stream_id="rover_rtk",
        solution_mode_filter=SolutionModeFilter.RTK_FIXED_FLOAT,
        outlier_factor=None,
    )
    assert metrics is not None
    assert metrics.fixed_ratio == pytest.approx(7 / 10)
    assert metrics.ttff_s == pytest.approx(3.0)


def test_rtk_fixed_float_ttff_none_when_no_fixed() -> None:
    """Если в RTK_FIXED_FLOAT-выборке нет ни одной FIXED — ttff_s = None,
    fixed_ratio = 0.0."""
    positions = [_shifted(d_lat_deg=i * 1e-7) for i in range(5)]
    epochs = [
        _make_epoch(
            position=p, time=_T0 + timedelta(seconds=i),
            solution_mode=SolutionMode.RTK_FLOAT,
        )
        for i, p in enumerate(positions)
    ]
    metrics = compute_metrics(
        epochs, _REFERENCE,
        session_id=1, stream_id="rover_rtk",
        solution_mode_filter=SolutionModeFilter.RTK_FIXED_FLOAT,
        outlier_factor=None,
    )
    assert metrics is not None
    assert metrics.fixed_ratio == pytest.approx(0.0)
    assert metrics.ttff_s is None


def test_filter_selects_only_target_solution_mode() -> None:
    """epochs_total включает все эпохи, epochs_after_filter — только
    отобранные по solution_mode_filter."""
    spp_positions = [_shifted(d_lat_deg=i * 1e-7) for i in range(4)]
    rtk_positions = [_shifted(d_lat_deg=i * 1e-7) for i in range(6)]
    epochs = [
                 _make_epoch(position=p, time=_T0 + timedelta(seconds=i),
                             solution_mode=SolutionMode.SPP)
                 for i, p in enumerate(spp_positions)
             ] + [
                 _make_epoch(position=p, time=_T0 + timedelta(seconds=10 + i),
                             solution_mode=SolutionMode.RTK_FIXED)
                 for i, p in enumerate(rtk_positions)
             ]
    metrics = compute_metrics(
        epochs, _REFERENCE,
        session_id=1, stream_id="rover_rtk",
        solution_mode_filter=SolutionModeFilter.RTK_FIXED,
        outlier_factor=None,
    )
    assert metrics is not None
    assert metrics.epochs_total == 10
    assert metrics.epochs_after_filter == 6


def test_skewness_positive_for_right_skewed_distribution() -> None:
    """Радиальные ошибки exp-распределения дают положительный skewness."""
    rng = np.random.default_rng(seed=42)
    # Радиальные ошибки exp(λ=1). Раскладываем на E и N через случайный угол.
    n_samples = 500
    radii = rng.exponential(scale=1e-3, size=n_samples)
    angles = rng.uniform(0.0, 2.0 * math.pi, size=n_samples)
    east_offsets_m = radii * np.cos(angles)
    north_offsets_m = radii * np.sin(angles)

    transformer = EnuTransformer.at(_REFERENCE)
    # Обратное приближение ENU→geodetic: для смещений ≤ 1 см влияние
    # кривизны Земли пренебрежимо мало по сравнению с поведением skewness.
    # Используем линейное приближение через малые приращения градусов.
    # Расстояние 1° по широте на этой широте ≈ 111.32 км; по долготе —
    # ≈ 111.32 · cos(φ) км.
    lat_rad = math.radians(_REFERENCE.latitude_deg)
    deg_per_m_north = 1.0 / 111_320.0
    deg_per_m_east = 1.0 / (111_320.0 * math.cos(lat_rad))

    positions = [
        GeodeticPosition(
            latitude_deg=_REFERENCE.latitude_deg + n_off * deg_per_m_north,
            longitude_deg=_REFERENCE.longitude_deg + e_off * deg_per_m_east,
            ellipsoidal_height_m=_REFERENCE.ellipsoidal_height_m,
        )
        for e_off, n_off in zip(east_offsets_m, north_offsets_m, strict=True)
    ]
    epochs = [
        _make_epoch(position=p, time=_T0 + timedelta(seconds=i))
        for i, p in enumerate(positions)
    ]
    metrics = compute_metrics(
        epochs, _REFERENCE,
        session_id=1, stream_id="rover_rtk",
        solution_mode_filter=SolutionModeFilter.RTK_FIXED,
        outlier_factor=None,
    )
    assert metrics is not None
    # Exp-распределение имеет теоретический skewness = 2.
    # На выборке 500 — допустимый разброс, проверяем знак и порядок.
    assert metrics.skewness_radial > 1.0
    # Заодно — transformer выше не использовался напрямую,
    # но импорт остался; помечаем как намеренно.
    _ = transformer


def test_computed_at_is_utc_aware() -> None:
    """computed_at по умолчанию — tz-aware UTC."""
    epochs = [_make_epoch(position=_REFERENCE, time=_T0)]
    metrics = compute_metrics(
        epochs, _REFERENCE,
        session_id=1, stream_id="rover_rtk",
        solution_mode_filter=SolutionModeFilter.RTK_FIXED,
        outlier_factor=None,
    )
    assert metrics is not None
    assert metrics.computed_at.tzinfo is not None
    assert metrics.computed_at.utcoffset() == timedelta(0)
