"""Расчёт метрик точности по бинам age_of_corrections_s.

Публичный API. Логика: для одной выборки (один сеанс, один канал, один
solution_mode_filter) эпохи группируются по интервалам возраста поправок
шириной bin_width_s; внутри каждого интервала считаются HRMS/VRMS/CEP50/R95.
Бины с числом эпох < min_epochs_per_bin помечаются is_significant=False, но из результата не вычёркиваются.

Для SPP биннинг не имеет смысла (age_of_corrections_s = None всегда) —
функция возвращает None.

Numpy здесь не импортируется — вся числовая часть в _age_bins_numerics.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from math import nan

from ntrip_accuracy_monitor.domain._age_bins_numerics import compute_bins_pipeline
from ntrip_accuracy_monitor.domain.epoch import Epoch
from ntrip_accuracy_monitor.domain.geodetic import EnuTransformer
from ntrip_accuracy_monitor.domain.metrics import SolutionModeFilter
from ntrip_accuracy_monitor.domain.position import GeodeticPosition
from ntrip_accuracy_monitor.domain.solution_mode import SolutionMode

_DEFAULT_BIN_WIDTH_S: float = 1.0
"""Шаг биннинга по умолчанию"""

_DEFAULT_MIN_EPOCHS_PER_BIN: int = 30
"""Минимум эпох для is_significant=True"""

_DEFAULT_OUTLIER_FACTOR: float = 5.0
"""Коэффициент порога выбраковки — тот же, что в compute_metrics."""

_BINNABLE_FILTERS: frozenset[SolutionModeFilter] = frozenset({
    SolutionModeFilter.DGNSS,
    SolutionModeFilter.RTK_FIXED,
    SolutionModeFilter.RTK_FIXED_FLOAT,
})


@dataclass(frozen=True, slots=True)
class AgeBinMetrics:
    """Один бин по age_of_corrections_s.

    Поля соответствуют колонкам таблицы metrics_by_age (схема БД 2/2)
    за вычетом bin_id/metrics_id — они появятся при записи в БД
    """

    age_bin_start_s: float
    age_bin_end_s: float
    epochs_count: int
    hrms_m: float
    vrms_m: float
    cep50_m: float
    r95_m: float
    is_significant: bool


@dataclass(frozen=True, slots=True)
class AgeBinMetricsSeries:
    """Серия бинов для одной выборки.

    Контекст выборки (session_id, stream_id, solution_mode_filter) хранится
    здесь, а не дублируется в каждом AgeBinMetrics. Параметры биннинга
    (bin_width_s, min_epochs_per_bin) — для самодокументирования результата.
    """

    session_id: int
    stream_id: str
    solution_mode_filter: SolutionModeFilter

    bin_width_s: float
    min_epochs_per_bin: int

    epochs_after_filter: int
    """Эпохи после фильтра по solution_mode_filter, до выбраковки."""

    epochs_rejected_outliers: int
    """Эпох выкинуто двухпроходной выбраковкой (0 если выбраковка отключена)."""

    epochs_with_valid_age: int
    """Эпохи, реально попавшие в биннинг (после выбраковки и отсева age=None/<0).
    Сумма epochs_count по всем бинам равна этому числу."""

    bins: tuple[AgeBinMetrics, ...]
    """Бины, отсортированные по age_bin_start_s. Пустые бины не возвращаются."""

    computed_at: datetime
    """Момент расчёта (UTC tz-aware)."""


def _filter_to_modes(value: SolutionModeFilter) -> frozenset[SolutionMode]:
    """SolutionModeFilter → набор значений SolutionMode для отбора эпох.

    Локальная копия из metrics.py: чтобы не делать публичным приватный хелпер
    другого модуля. Дублирование тривиальное (одно match-выражение).
    """
    match value:
        case SolutionModeFilter.SPP:
            return frozenset({SolutionMode.SPP})
        case SolutionModeFilter.DGNSS:
            return frozenset({SolutionMode.DGNSS})
        case SolutionModeFilter.RTK_FIXED:
            return frozenset({SolutionMode.RTK_FIXED})
        case SolutionModeFilter.RTK_FIXED_FLOAT:
            return frozenset({SolutionMode.RTK_FIXED, SolutionMode.RTK_FLOAT})


def compute_age_bin_metrics(
    epochs: Sequence[Epoch],
    reference: GeodeticPosition,
    *,
    session_id: int,
    stream_id: str,
    solution_mode_filter: SolutionModeFilter,
    bin_width_s: float = _DEFAULT_BIN_WIDTH_S,
    min_epochs_per_bin: int = _DEFAULT_MIN_EPOCHS_PER_BIN,
    outlier_factor: float | None = _DEFAULT_OUTLIER_FACTOR,
    computed_at: datetime | None = None,
) -> AgeBinMetricsSeries | None:
    """Биннинг метрик точности по age_of_corrections_s для одной выборки.

        1. фильтр по solution_mode_filter;
        2. ENU через EnuTransformer.at(reference);
        3. двухпроходная выбраковка по горизонтальной радиальной ошибке
           (повторяется локально, чтобы получить keep_mask для синхронной
           фильтрации параллельного массива age);
        4. отсев эпох с age_of_corrections_s = None или < 0;
        5. группировка по floor(age / bin_width_s);
        6. HRMS/VRMS/CEP50/R95 внутри каждого бина;
        7. is_significant = (epochs_count >= min_epochs_per_bin).

    Args:
        epochs: эпохи одного канала за сеанс, отсортированные по времени.
            Контракт сортировки нужен только для устойчивости порядка
            внутри бина; биннинг сам по себе порядка не требует.
        reference: эталонная геодезическая точка (центр ENU).
        session_id, stream_id: для заполнения AgeBinMetricsSeries; функция
            их не валидирует.
        solution_mode_filter: какие эпохи отбирать. SPP вернёт None.
        bin_width_s: ширина бина в секундах. По умолчанию 1.0 (методика).
        min_epochs_per_bin: порог значимости. По умолчанию 30 (методика).
        outlier_factor: тот же, что в compute_metrics. None отключает выбраковку.
        computed_at: момент расчёта. None → datetime.now(timezone.utc).

    Returns:
        AgeBinMetricsSeries или None, если:
            - solution_mode_filter == SPP;
            - после фильтра по режиму выборка пуста;
            - после выбраковки выборка пуста;
            - после отсева невалидного age нет ни одной эпохи для биннинга.

    Raises:
        ValueError: если bin_width_s <= 0, min_epochs_per_bin < 0 либо
                    outlier_factor задан и <= 0.
    """
    if bin_width_s <= 0:
        raise ValueError(
            f"bin_width_s must be positive, got {bin_width_s!r}"
        )
    if min_epochs_per_bin < 0:
        raise ValueError(
            f"min_epochs_per_bin must be non-negative, got {min_epochs_per_bin!r}"
        )
    if outlier_factor is not None and outlier_factor <= 0:
        raise ValueError(
            f"outlier_factor must be positive or None, got {outlier_factor!r}"
        )

    if solution_mode_filter not in _BINNABLE_FILTERS:
        return None

    if computed_at is None:
        computed_at = datetime.now(timezone.utc)

    target_modes = _filter_to_modes(solution_mode_filter)
    filtered = [e for e in epochs if e.solution_mode in target_modes]
    epochs_after_filter = len(filtered)
    if epochs_after_filter == 0:
        return None

    transformer = EnuTransformer.at(reference)
    east_list: list[float] = []
    north_list: list[float] = []
    up_list: list[float] = []
    age_list: list[float] = []
    for epoch in filtered:
        offset = transformer.to_enu(epoch.position)
        east_list.append(offset.east_m)
        north_list.append(offset.north_m)
        up_list.append(offset.up_m)
        age = epoch.age_of_corrections_s
        age_list.append(age if age is not None else nan)

    pipeline = compute_bins_pipeline(
        east_list=east_list,
        north_list=north_list,
        up_list=up_list,
        age_list=age_list,
        bin_width_s=bin_width_s,
        outlier_factor=outlier_factor,
    )

    if pipeline.epochs_with_valid_age == 0:
        return None

    bins = tuple(
        AgeBinMetrics(
            age_bin_start_s=br.age_start_s,
            age_bin_end_s=br.age_end_s,
            epochs_count=br.epochs_count,
            hrms_m=br.hrms_m,
            vrms_m=br.vrms_m,
            cep50_m=br.cep50_m,
            r95_m=br.r95_m,
            is_significant=br.epochs_count >= min_epochs_per_bin,
        )
        for br in pipeline.bins
    )

    return AgeBinMetricsSeries(
        session_id=session_id,
        stream_id=stream_id,
        solution_mode_filter=solution_mode_filter,
        bin_width_s=bin_width_s,
        min_epochs_per_bin=min_epochs_per_bin,
        epochs_after_filter=epochs_after_filter,
        epochs_rejected_outliers=pipeline.epochs_rejected_outliers,
        epochs_with_valid_age=pipeline.epochs_with_valid_age,
        bins=bins,
        computed_at=computed_at,
    )
