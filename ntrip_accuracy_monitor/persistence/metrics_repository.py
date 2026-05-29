"""Репозиторий метрик точности для таблицы session_metrics.

upsert использует ON CONFLICT DO UPDATE по UNIQUE(session_id, stream_id,
solution_mode_filter): metrics_id стабилен между пересчётами, что важно
для FK metrics_by_age.metrics_id.

outlier_factor / age_bin_width_s / age_bin_min_epochs принимаются kw-only
параметрами, а не как поля AccuracyMetrics: это persistence-метаданные
расчёта. При чтении fetch_* возвращают AccuracyMetrics без них; параметры
биннинга нужны только AgeBinMetricsRepository.fetch_for_metrics для
восстановления AgeBinMetricsSeries.
"""

from __future__ import annotations

from typing import Final

import asyncpg

from ntrip_accuracy_monitor.domain.metrics import (
    AccuracyMetrics,
    SolutionModeFilter,
)
from ntrip_accuracy_monitor.persistence._executor import (
    Executor,
    acquire_connection,
)

_INSERT_COLUMNS: Final = (
    "session_id",
    "stream_id",
    "solution_mode_filter",
    "epochs_total",
    "epochs_after_filter",
    "epochs_rejected_outliers",
    "hrms_m",
    "vrms_m",
    "two_drms_m",
    "cep50_m",
    "r95_m",
    "three_drms_m",
    "error_3d_mean_m",
    "error_3d_max_m",
    "fixed_ratio",
    "ttff_s",
    "skewness_radial",
    "outlier_factor",
    "age_bin_width_s",
    "age_bin_min_epochs",
    "computed_at",
)

_PLAIN_UPDATE_COLUMNS: Final = tuple(
    c for c in _INSERT_COLUMNS
    if c not in {
        "session_id", "stream_id", "solution_mode_filter",
        "age_bin_width_s", "age_bin_min_epochs",
    }
)

_UPSERT_SQL: Final = (
    f"INSERT INTO session_metrics ({', '.join(_INSERT_COLUMNS)})\n"
    f"VALUES ({', '.join(f'${i + 1}' for i in range(len(_INSERT_COLUMNS)))})\n"
    "ON CONFLICT (session_id, stream_id, solution_mode_filter)\n"
    "DO UPDATE SET\n"
    + ",\n".join(f"    {c} = EXCLUDED.{c}" for c in _PLAIN_UPDATE_COLUMNS)
    + ",\n"
      "    age_bin_width_s = COALESCE("
      "EXCLUDED.age_bin_width_s, session_metrics.age_bin_width_s),\n"
      "    age_bin_min_epochs = COALESCE("
      "EXCLUDED.age_bin_min_epochs, session_metrics.age_bin_min_epochs)\n"
      "RETURNING metrics_id"
)

_FETCH_SELECT: Final = """\
SELECT session_id, stream_id, solution_mode_filter,
       epochs_total, epochs_after_filter, epochs_rejected_outliers,
       hrms_m, vrms_m, two_drms_m, cep50_m, r95_m, three_drms_m,
       error_3d_mean_m, error_3d_max_m,
       fixed_ratio, ttff_s,
       skewness_radial, computed_at
"""

_FETCH_FOR_SESSION_SQL: Final = (
    _FETCH_SELECT
    + "FROM session_metrics\n"
    + "WHERE session_id = $1 AND stream_id = $2\n"
    + "ORDER BY solution_mode_filter"
)

_FETCH_ONE_SQL: Final = (
    _FETCH_SELECT
    + "FROM session_metrics\n"
    + "WHERE session_id = $1 AND stream_id = $2 AND solution_mode_filter = $3"
)


class MetricsRepository:
    """CRUD таблицы session_metrics."""

    def __init__(self, executor: Executor) -> None:
        self._executor = executor

    async def upsert(
        self,
        metrics: AccuracyMetrics,
        *,
        outlier_factor: float | None,
        age_bin_width_s: float | None = None,
        age_bin_min_epochs: int | None = None,
        conn: asyncpg.Connection | None = None,
    ) -> int:
        """Вставить или обновить метрики; вернуть стабильный metrics_id.

        outlier_factor: с каким порогом считались метрики (None — выбраковка
            отключена). Хранится для аудита, не входит в AccuracyMetrics.
        age_bin_width_s, age_bin_min_epochs: параметры биннинга. None если
            эти метрики не сопровождаются age-bin расчётом — иначе требуются,
            чтобы AgeBinMetricsRepository.fetch_for_metrics мог точно
            восстановить AgeBinMetricsSeries.
        conn: если задан, операция выполняется в существующей транзакции
            (для persist=True в MetricsService upsert + insert_series должны
            быть атомарны). Иначе репозиторий берёт коннект из executor.
        """
        values = (
            metrics.session_id,
            metrics.stream_id,
            metrics.solution_mode_filter.value,
            metrics.epochs_total,
            metrics.epochs_after_filter,
            metrics.epochs_rejected_outliers,
            metrics.hrms_m,
            metrics.vrms_m,
            metrics.two_drms_m,
            metrics.cep50_m,
            metrics.r95_m,
            metrics.three_drms_m,
            metrics.error_3d_mean_m,
            metrics.error_3d_max_m,
            metrics.fixed_ratio,
            metrics.ttff_s,
            metrics.skewness_radial,
            outlier_factor,
            age_bin_width_s,
            age_bin_min_epochs,
            metrics.computed_at,
        )

        executor: Executor = conn if conn is not None else self._executor
        async with acquire_connection(executor) as c:
            row = await c.fetchrow(_UPSERT_SQL, *values)

        # ON CONFLICT DO UPDATE с RETURNING всегда возвращает строку.
        assert row is not None
        return int(row["metrics_id"])

    async def fetch_for_session(
        self,
        session_id: int,
        stream_id: str,
        *,
        conn: asyncpg.Connection | None = None,
    ) -> list[AccuracyMetrics]:
        """Все строки метрик для (session, stream); сортировка по filter."""
        executor: Executor = conn if conn is not None else self._executor
        async with acquire_connection(executor) as c:
            rows = await c.fetch(_FETCH_FOR_SESSION_SQL, session_id, stream_id)
        return [self._row_to_metrics(r) for r in rows]

    async def fetch_one(
        self,
        session_id: int,
        stream_id: str,
        solution_mode_filter: SolutionModeFilter,
        *,
        conn: asyncpg.Connection | None = None,
    ) -> AccuracyMetrics | None:
        """Конкретная строка метрик или None."""
        executor: Executor = conn if conn is not None else self._executor
        async with acquire_connection(executor) as c:
            row = await c.fetchrow(
                _FETCH_ONE_SQL,
                session_id,
                stream_id,
                solution_mode_filter.value,
            )
        if row is None:
            return None
        return self._row_to_metrics(row)

    @staticmethod
    def _row_to_metrics(row: asyncpg.Record) -> AccuracyMetrics:
        """Маппинг asyncpg.Record → AccuracyMetrics.

        outlier_factor / age_bin_width_s / age_bin_min_epochs остаются в БД
        как метаданные persistence и в AccuracyMetrics не попадают.
        """
        return AccuracyMetrics(
            session_id=row["session_id"],
            stream_id=row["stream_id"],
            solution_mode_filter=SolutionModeFilter(row["solution_mode_filter"]),
            epochs_total=row["epochs_total"],
            epochs_after_filter=row["epochs_after_filter"],
            epochs_rejected_outliers=row["epochs_rejected_outliers"],
            hrms_m=row["hrms_m"],
            vrms_m=row["vrms_m"],
            two_drms_m=row["two_drms_m"],
            cep50_m=row["cep50_m"],
            r95_m=row["r95_m"],
            three_drms_m=row["three_drms_m"],
            error_3d_mean_m=row["error_3d_mean_m"],
            error_3d_max_m=row["error_3d_max_m"],
            fixed_ratio=row["fixed_ratio"],
            ttff_s=row["ttff_s"],
            skewness_radial=row["skewness_radial"],
            computed_at=row["computed_at"],
        )
