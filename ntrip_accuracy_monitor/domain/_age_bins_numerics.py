"""Числовые вычисления для биннинга метрик точности по age_of_corrections_s.

Приватный модуль: вся numpy-математика биннинга. Публичный domain/age_bins.py
не импортирует numpy — развязка по той же схеме, что и
domain/_metrics_numerics.py ↔ domain/metrics.py
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from numpy.typing import NDArray

type FloatArray = NDArray[np.float64]
type BoolArray = NDArray[np.bool_]

_R95_METHOD: str = "hazen"
"""Метод оценки 95-го квантиля — согласован с _metrics_numerics.py"""


@dataclass(frozen=True, slots=True)
class BinNumericalResult:
    """Один бин: интервал по age и метрики внутри.

    Поля совпадают с колонками таблицы metrics_by_age, кроме
    is_significant (его вычисляет публичный слой по min_epochs_per_bin)
    и bin_id/metrics_id (они появятся при записи в БД, миграция V004).
    """

    age_start_s: float
    age_end_s: float
    epochs_count: int
    hrms_m: float
    vrms_m: float
    cep50_m: float
    r95_m: float


@dataclass(frozen=True, slots=True)
class BinningPipelineResult:
    """Результат полного конвейера: выбраковка → отсев невалидного age → биннинг."""

    epochs_rejected_outliers: int
    """Сколько эпох выкинула двухпроходная выбраковка (0 если она отключена)."""

    epochs_with_valid_age: int
    """Сколько эпох участвовало в биннинге (после выбраковки и отсева age=NaN/<0)."""

    bins: list[BinNumericalResult]
    """Бины, отсортированные по age_start_s. Пустые бины не возвращаются."""


def compute_bins_pipeline(
    east_list: list[float],
    north_list: list[float],
    up_list: list[float],
    age_list: list[float],
    bin_width_s: float,
    outlier_factor: float | None,
) -> BinningPipelineResult:
    """Полный конвейер биннинга.

    Args:
        east_list, north_list, up_list: ENU-смещения эпох после фильтра
            по solution_mode_filter, в метрах, одинаковой длины.
        age_list: age_of_corrections_s параллельным массивом. Для эпох
            без age передаётся float("nan") — отсеется внутри.
        bin_width_s: ширина бина в секундах, > 0.
        outlier_factor: коэффициент порога выбраковки по horizontal radial;
            None — выбраковка отключена.

    Returns:
        BinningPipelineResult. Если после выбраковки или после отсева age
        не осталось ни одной эпохи — bins=[] и epochs_with_valid_age=0.

    Бины — полуоткрытые интервалы [age_start_s, age_end_s):
        bin_index = floor(age / bin_width_s)
        age_start_s = bin_index * bin_width_s
        age_end_s   = age_start_s + bin_width_s
    """
    east_arr = np.asarray(east_list, dtype=np.float64)
    north_arr = np.asarray(north_list, dtype=np.float64)
    up_arr = np.asarray(up_list, dtype=np.float64)
    age_arr = np.asarray(age_list, dtype=np.float64)

    rejected_count = 0
    if outlier_factor is not None and east_arr.size > 0:
        keep_mask = _compute_outlier_keep_mask(east_arr, north_arr, outlier_factor)
        rejected_count = int((~keep_mask).sum())
        east_arr = east_arr[keep_mask]
        north_arr = north_arr[keep_mask]
        up_arr = up_arr[keep_mask]
        age_arr = age_arr[keep_mask]

    if east_arr.size == 0:
        return BinningPipelineResult(
            epochs_rejected_outliers=rejected_count,
            epochs_with_valid_age=0,
            bins=[],
        )

    valid_age_mask = ~np.isnan(age_arr) & (age_arr >= 0.0)
    east_arr = east_arr[valid_age_mask]
    north_arr = north_arr[valid_age_mask]
    up_arr = up_arr[valid_age_mask]
    age_arr = age_arr[valid_age_mask]
    epochs_with_valid_age = int(east_arr.size)

    if epochs_with_valid_age == 0:
        return BinningPipelineResult(
            epochs_rejected_outliers=rejected_count,
            epochs_with_valid_age=0,
            bins=[],
        )

    bins = _compute_bins(east_arr, north_arr, up_arr, age_arr, bin_width_s)
    return BinningPipelineResult(
        epochs_rejected_outliers=rejected_count,
        epochs_with_valid_age=epochs_with_valid_age,
        bins=bins,
    )


def _compute_outlier_keep_mask(
    east_m: FloatArray,
    north_m: FloatArray,
    outlier_factor: float,
) -> BoolArray:
    """Маска "оставить" для выбраковки по horizontal radial > factor·HRMS_first_pass.

    Повторяет логику apply_outlier_rejection из _metrics_numerics.py, но
    возвращает именно маску — она нужна, чтобы синхронно отфильтровать
    параллельный массив age_s.
    """
    horizontal_squared = east_m ** 2 + north_m ** 2
    hrms_first_pass = float(np.sqrt(np.mean(horizontal_squared)))
    if hrms_first_pass <= 0.0:
        return np.ones(east_m.shape, dtype=np.bool_)
    threshold = outlier_factor * hrms_first_pass
    radial = np.sqrt(horizontal_squared)
    return radial <= threshold


def _compute_bins(
    east_m: FloatArray,
    north_m: FloatArray,
    up_m: FloatArray,
    age_s: FloatArray,
    bin_width_s: float,
) -> list[BinNumericalResult]:
    """Группировка по floor(age / bin_width_s) и расчёт метрик в каждом бине."""
    bin_indices = np.floor(age_s / bin_width_s).astype(np.int64)
    unique_indices = np.unique(bin_indices)
    results: list[BinNumericalResult] = []
    for bin_idx in unique_indices:
        mask = bin_indices == bin_idx
        bin_east = east_m[mask]
        bin_north = north_m[mask]
        bin_up = up_m[mask]
        metrics = _compute_bin_metrics(bin_east, bin_north, bin_up)
        age_start = float(bin_idx) * bin_width_s
        results.append(
            BinNumericalResult(
                age_start_s=age_start,
                age_end_s=age_start + bin_width_s,
                epochs_count=int(mask.sum()),
                hrms_m=metrics[0],
                vrms_m=metrics[1],
                cep50_m=metrics[2],
                r95_m=metrics[3],
            )
        )
    return results


def _compute_bin_metrics(
    east_m: FloatArray,
    north_m: FloatArray,
    up_m: FloatArray,
) -> tuple[float, float, float, float]:
    """HRMS, VRMS, CEP50, R95 по одному бину. Бин гарантированно непуст."""
    horizontal_squared = east_m ** 2 + north_m ** 2
    horizontal_radial = np.sqrt(horizontal_squared)
    hrms_m = float(np.sqrt(np.mean(horizontal_squared)))
    vrms_m = float(np.sqrt(np.mean(up_m ** 2)))
    cep50_m = float(np.median(horizontal_radial))
    r95_m = float(np.percentile(horizontal_radial, 95.0, method=_R95_METHOD))
    return hrms_m, vrms_m, cep50_m, r95_m
