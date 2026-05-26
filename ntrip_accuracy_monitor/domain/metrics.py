"""Расчёт метрик точности позиционирования по выборке эпох."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum

from ntrip_accuracy_monitor.domain._metrics_numerics import (
    apply_outlier_rejection,
    compute_numerical_stats,
    offsets_to_arrays,
)
from ntrip_accuracy_monitor.domain.epoch import Epoch
from ntrip_accuracy_monitor.domain.geodetic import EnuTransformer
from ntrip_accuracy_monitor.domain.position import GeodeticPosition
from ntrip_accuracy_monitor.domain.solution_mode import SolutionMode

_DEFAULT_OUTLIER_FACTOR: float = 5.0
"""Коэффициент порога выбраковки выбросов из раздела 8.4 методики."""


class SolutionModeFilter(StrEnum):
    """Класс выборки для расчёта метрик.

    Соответствует значениям колонки session_metrics.solution_mode_filter
    (VARCHAR(24)). Это не дубликат SolutionMode — у него другая
    семантика: SolutionMode — режим одной эпохи, SolutionModeFilter —
    правило отбора эпох в выборку, включая синтетическую комбинацию
    RTK_FIXED_FLOAT (quality ∈ {4, 5}).
    """

    SPP = "SPP"
    DGNSS = "DGNSS"
    RTK_FIXED = "RTK_FIXED"
    RTK_FIXED_FLOAT = "RTK_FIXED_FLOAT"


@dataclass(frozen=True, slots=True)
class AccuracyMetrics:
    """Метрики точности по одному режиму в одном канале одного сеанса."""

    session_id: int
    stream_id: str
    solution_mode_filter: SolutionModeFilter

    epochs_total: int
    """Все эпохи канала в сеансе (до фильтра по режиму). Интерпретация A."""

    epochs_after_filter: int
    """Эпохи после фильтра по solution_mode_filter, до выбраковки выбросов."""

    epochs_rejected_outliers: int
    """Эпох выкинуто на втором проходе выбраковки (0 если выбраковка отключена)."""

    hrms_m: float
    vrms_m: float
    two_drms_m: float
    cep50_m: float
    r95_m: float
    three_drms_m: float

    error_3d_mean_m: float
    """Mean 3D-ошибки. Не входит в session_metrics; для отчёта."""

    error_3d_max_m: float
    """Max 3D-ошибки. Не входит в session_metrics; для отчёта."""

    fixed_ratio: float | None
    """Для RTK_FIXED_FLOAT: N_RTK_FIXED / (N_RTK_FIXED + N_RTK_FLOAT)
    по выборке ДО выбраковки. Для остальных фильтров — None."""

    ttff_s: float | None
    """Для RTK_FIXED_FLOAT: секунды от первой эпохи выборки до первой
    эпохи с RTK_FIXED. None если фильтр не RTK_FIXED_FLOAT либо в
    выборке нет ни одной RTK_FIXED."""

    skewness_radial: float
    """Fisher–Pearson skewness горизонтальной радиальной ошибки.
    Возвращает 0.0 для N<2 или вырожденной выборки (σ=0)."""

    computed_at: datetime
    """Момент расчёта (UTC tz-aware)."""


def _filter_to_modes(value: SolutionModeFilter) -> frozenset[SolutionMode]:
    """SolutionModeFilter → набор значений SolutionMode для отбора эпох."""
    match value:
        case SolutionModeFilter.SPP:
            return frozenset({SolutionMode.SPP})
        case SolutionModeFilter.DGNSS:
            return frozenset({SolutionMode.DGNSS})
        case SolutionModeFilter.RTK_FIXED:
            return frozenset({SolutionMode.RTK_FIXED})
        case SolutionModeFilter.RTK_FIXED_FLOAT:
            return frozenset({SolutionMode.RTK_FIXED, SolutionMode.RTK_FLOAT})


def _compute_ttff_and_fixed_ratio(
        filtered_epochs: Sequence[Epoch],
) -> tuple[float | None, float | None]:
    """fixed_ratio и ttff_s для случая RTK_FIXED_FLOAT.

    Предполагается, что эпохи отсортированы по epoch_time (контракт
    EpochRepository.fetch_for_session_stream — ORDER BY epoch_time).
    """
    n_total = len(filtered_epochs)
    if n_total == 0:
        return None, None
    n_fixed = sum(
        1 for e in filtered_epochs if e.solution_mode is SolutionMode.RTK_FIXED
    )
    fixed_ratio = n_fixed / n_total

    ttff_s: float | None = None
    first_time = filtered_epochs[0].epoch_time
    for epoch in filtered_epochs:
        if epoch.solution_mode is SolutionMode.RTK_FIXED:
            ttff_s = (epoch.epoch_time - first_time).total_seconds()
            break

    return fixed_ratio, ttff_s


def compute_metrics(
        epochs: Sequence[Epoch],
        reference: GeodeticPosition,
        *,
        session_id: int,
        stream_id: str,
        solution_mode_filter: SolutionModeFilter,
        outlier_factor: float | None = _DEFAULT_OUTLIER_FACTOR,
        computed_at: datetime | None = None,
) -> AccuracyMetrics | None:
    """Вычислить метрики точности для одной выборки.

    Args:
        epochs: эпохи одного канала за сеанс, отсортированные по времени.
            Может содержать эпохи разных solution_mode — фильтр применится
            внутри функции.
        reference: эталонная геодезическая точка (центр ENU).
        session_id, stream_id: для заполнения соответствующих полей
            AccuracyMetrics; функция их не валидирует.
        solution_mode_filter: какие эпохи отбирать.
        outlier_factor: коэффициент порога выбраковки. None — выбраковка
            отключена. По умолчанию 5.0 (методика, раздел 8.4).
        computed_at: момент расчёта. None → datetime.now(timezone.utc).

    Returns:
        AccuracyMetrics или None, если после фильтра по режиму либо после
        выбраковки в выборке не осталось эпох.

    Raises:
        ValueError: outlier_factor задан и не-положителен.
    """
    if outlier_factor is not None and outlier_factor <= 0:
        raise ValueError(
            f"outlier_factor must be positive or None, got {outlier_factor!r}"
        )

    if computed_at is None:
        computed_at = datetime.now(timezone.utc)

    epochs_total = len(epochs)
    if epochs_total == 0:
        return None

    target_modes = _filter_to_modes(solution_mode_filter)
    filtered = [e for e in epochs if e.solution_mode in target_modes]
    epochs_after_filter = len(filtered)
    if epochs_after_filter == 0:
        return None

    fixed_ratio: float | None = None
    ttff_s: float | None = None
    if solution_mode_filter is SolutionModeFilter.RTK_FIXED_FLOAT:
        fixed_ratio, ttff_s = _compute_ttff_and_fixed_ratio(filtered)

    transformer = EnuTransformer.at(reference)
    east_list: list[float] = []
    north_list: list[float] = []
    up_list: list[float] = []
    for epoch in filtered:
        offset = transformer.to_enu(epoch.position)
        east_list.append(offset.east_m)
        north_list.append(offset.north_m)
        up_list.append(offset.up_m)

    east_arr, north_arr, up_arr = offsets_to_arrays(east_list, north_list, up_list)

    epochs_rejected_outliers = 0
    if outlier_factor is not None:
        rejection = apply_outlier_rejection(
            east_arr, north_arr, up_arr, outlier_factor,
        )
        east_arr = rejection.east_m
        north_arr = rejection.north_m
        up_arr = rejection.up_m
        epochs_rejected_outliers = rejection.rejected_count

    if east_arr.size == 0:
        return None

    stats = compute_numerical_stats(east_arr, north_arr, up_arr)

    return AccuracyMetrics(
        session_id=session_id,
        stream_id=stream_id,
        solution_mode_filter=solution_mode_filter,
        epochs_total=epochs_total,
        epochs_after_filter=epochs_after_filter,
        epochs_rejected_outliers=epochs_rejected_outliers,
        hrms_m=stats.hrms_m,
        vrms_m=stats.vrms_m,
        two_drms_m=stats.two_drms_m,
        cep50_m=stats.cep50_m,
        r95_m=stats.r95_m,
        three_drms_m=stats.three_drms_m,
        error_3d_mean_m=stats.error_3d_mean_m,
        error_3d_max_m=stats.error_3d_max_m,
        fixed_ratio=fixed_ratio,
        ttff_s=ttff_s,
        skewness_radial=stats.skewness_radial,
        computed_at=computed_at,
    )
