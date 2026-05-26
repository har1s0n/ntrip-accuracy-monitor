"""Числовые вычисления для расчёта метрик точности (приватный модуль)."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from numpy.typing import NDArray

type FloatArray = NDArray[np.float64]

_R95_METHOD: str = "hazen"
"""Метод оценки 95-го квантиля — согласован с open-source-стеком проекта."""


@dataclass(frozen=True, slots=True)
class OutlierRejectionResult:
    """Результат двухпроходной выбраковки по горизонтальной радиальной ошибке."""

    east_m: FloatArray
    north_m: FloatArray
    up_m: FloatArray
    rejected_count: int


@dataclass(frozen=True, slots=True)
class NumericalStats:
    """Числовая часть AccuracyMetrics. Все значения — метры, кроме безразмерного skewness."""

    hrms_m: float
    vrms_m: float
    two_drms_m: float
    cep50_m: float
    r95_m: float
    three_drms_m: float
    error_3d_mean_m: float
    error_3d_max_m: float
    skewness_radial: float


def offsets_to_arrays(
    east: list[float],
    north: list[float],
    up: list[float],
) -> tuple[FloatArray, FloatArray, FloatArray]:
    """Списки float → три np.float64 массива одинаковой длины."""
    return (
        np.asarray(east, dtype=np.float64),
        np.asarray(north, dtype=np.float64),
        np.asarray(up, dtype=np.float64),
    )


def apply_outlier_rejection(
    east_m: FloatArray,
    north_m: FloatArray,
    up_m: FloatArray,
    outlier_factor: float,
) -> OutlierRejectionResult:
    """Двухпроходная выбраковка эпох с радиальной ошибкой > factor·HRMS_first.

    Если HRMS первого прохода нулевой (все эпохи в одной точке) или ни одна
    эпоха не превышает порог — массивы возвращаются как есть, rejected_count=0.
    """
    horizontal_squared = east_m ** 2 + north_m ** 2
    hrms_first_pass = float(np.sqrt(np.mean(horizontal_squared)))
    if hrms_first_pass <= 0.0:
        return OutlierRejectionResult(
            east_m=east_m, north_m=north_m, up_m=up_m, rejected_count=0
        )
    threshold = outlier_factor * hrms_first_pass
    radial = np.sqrt(horizontal_squared)
    keep_mask = radial <= threshold
    rejected_count = int(np.sum(~keep_mask))
    if rejected_count == 0:
        return OutlierRejectionResult(
            east_m=east_m, north_m=north_m, up_m=up_m, rejected_count=0
        )
    return OutlierRejectionResult(
        east_m=east_m[keep_mask],
        north_m=north_m[keep_mask],
        up_m=up_m[keep_mask],
        rejected_count=rejected_count,
    )


def compute_numerical_stats(
    east_m: FloatArray,
    north_m: FloatArray,
    up_m: FloatArray,
) -> NumericalStats:
    """Все числовые метрики по уже отфильтрованной (после выбраковки) выборке."""
    horizontal_squared = east_m ** 2 + north_m ** 2
    horizontal_radial = np.sqrt(horizontal_squared)
    error_3d_squared = horizontal_squared + up_m ** 2
    error_3d = np.sqrt(error_3d_squared)

    hrms_m = float(np.sqrt(np.mean(horizontal_squared)))
    vrms_m = float(np.sqrt(np.mean(up_m ** 2)))
    cep50_m = float(np.median(horizontal_radial))
    r95_m = float(np.percentile(horizontal_radial, 95.0, method=_R95_METHOD))
    three_drms_m = float(np.sqrt(np.mean(error_3d_squared)))
    error_3d_mean_m = float(np.mean(error_3d))
    error_3d_max_m = float(np.max(error_3d))
    skewness_radial = _fisher_pearson_skewness(horizontal_radial)

    return NumericalStats(
        hrms_m=hrms_m,
        vrms_m=vrms_m,
        two_drms_m=2.0 * hrms_m,
        cep50_m=cep50_m,
        r95_m=r95_m,
        three_drms_m=three_drms_m,
        error_3d_mean_m=error_3d_mean_m,
        error_3d_max_m=error_3d_max_m,
        skewness_radial=skewness_radial,
    )


def _fisher_pearson_skewness(values: FloatArray) -> float:
    """g₁ = (1/N) · Σ ((x_i - μ)/σ)³. Для N<2 или σ=0 возвращает 0.0."""
    n = values.size
    if n < 2:
        return 0.0
    mean = float(np.mean(values))
    std = float(np.std(values, ddof=0))
    if std == 0.0:
        return 0.0
    z_values = (values - mean) / std
    return float(np.mean(z_values ** 3))
