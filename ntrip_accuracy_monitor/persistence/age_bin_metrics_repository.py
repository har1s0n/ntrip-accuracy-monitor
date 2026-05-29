"""Репозиторий age-bin метрик для таблицы metrics_by_age.

insert_series по сути — replace: DELETE существующих bins для metrics_id
+ COPY новых, всё в одной транзакции. Это нужно потому что ON CONFLICT
по (metrics_id, age_bin_start_s) не покрывает случай, когда новый набор
бинов МЕНЬШЕ старого (например, пересчитали с большим bin_width_s) —
старые "висящие" bins остались бы.

fetch_for_metrics достаёт контекст серии JOIN-ом из session_metrics:
session_id, stream_id, solution_mode_filter, age_bin_width_s,
age_bin_min_epochs, epochs_after_filter, epochs_rejected_outliers,
computed_at. Колонки age_bin_width_s / age_bin_min_epochs ОБЯЗАНЫ быть
проставлены, иначе ValueError — без них восстановить AgeBinMetricsSeries
нельзя (min_epochs_per_bin из данных не восстановим).
"""

from __future__ import annotations

from typing import Final

import asyncpg

from ntrip_accuracy_monitor.domain.age_bins import (
    AgeBinMetrics,
    AgeBinMetricsSeries,
)
from ntrip_accuracy_monitor.domain.metrics import SolutionModeFilter
from ntrip_accuracy_monitor.persistence._executor import (
    Executor,
    acquire_connection,
)

_COPY_COLUMNS: Final = (
    "metrics_id",
    "age_bin_start_s",
    "age_bin_end_s",
    "epochs_count",
    "hrms_m",
    "vrms_m",
    "cep50_m",
    "r95_m",
    "is_significant",
)

_DELETE_FOR_METRICS_SQL: Final = """\
DELETE FROM metrics_by_age WHERE metrics_id = $1
"""

# JOIN вытаскивает контекст AgeBinMetricsSeries из session_metrics.
# LEFT JOIN — на случай, когда строка метрик есть, а bins пусто (теоретически
# возможно, если bins=() было записано; compute_age_bin_metrics так не делает).
_FETCH_FOR_METRICS_SQL: Final = """\
SELECT
    sm.session_id,
    sm.stream_id,
    sm.solution_mode_filter,
    sm.epochs_after_filter,
    sm.epochs_rejected_outliers,
    sm.age_bin_width_s,
    sm.age_bin_min_epochs,
    sm.computed_at,
    a.age_bin_start_s,
    a.age_bin_end_s,
    a.epochs_count,
    a.hrms_m,
    a.vrms_m,
    a.cep50_m,
    a.r95_m,
    a.is_significant
FROM session_metrics sm
LEFT JOIN metrics_by_age a ON a.metrics_id = sm.metrics_id
WHERE sm.metrics_id = $1
ORDER BY a.age_bin_start_s NULLS LAST
"""


class AgeBinMetricsRepository:
    """CRUD таблицы metrics_by_age."""

    def __init__(self, executor: Executor) -> None:
        self._executor = executor

    async def insert_series(
        self,
        series: AgeBinMetricsSeries,
        metrics_id: int,
        *,
        conn: asyncpg.Connection | None = None,
    ) -> None:
        """Заменить весь набор bins для metrics_id (DELETE + COPY).

        Атомарно в одной транзакции — либо новый набор целиком, либо
        старый сохранён. Если conn передан и обёрнут в .transaction()
        вызывающим, asyncpg откроет savepoint — это безопасно.
        """
        records = tuple(
            (
                metrics_id,
                bin_.age_bin_start_s,
                bin_.age_bin_end_s,
                bin_.epochs_count,
                bin_.hrms_m,
                bin_.vrms_m,
                bin_.cep50_m,
                bin_.r95_m,
                bin_.is_significant,
            )
            for bin_ in series.bins
        )

        executor: Executor = conn if conn is not None else self._executor
        async with acquire_connection(executor) as c:
            async with c.transaction():
                await c.execute(_DELETE_FOR_METRICS_SQL, metrics_id)
                if records:
                    await c.copy_records_to_table(
                        "metrics_by_age",
                        records=records,
                        columns=_COPY_COLUMNS,
                    )

    async def fetch_for_metrics(
        self,
        metrics_id: int,
        *,
        conn: asyncpg.Connection | None = None,
    ) -> AgeBinMetricsSeries | None:
        """Восстановить AgeBinMetricsSeries из БД или None если строки нет.

        Контекст серии — из session_metrics; age_bin_width_s и
        age_bin_min_epochs обязаны быть заполнены, иначе ValueError:
        они NULL только если age-bin расчёт для этой строки не делался,
        и тогда восстанавливать нечего.
        """
        executor: Executor = conn if conn is not None else self._executor
        async with acquire_connection(executor) as c:
            rows = await c.fetch(_FETCH_FOR_METRICS_SQL, metrics_id)

        if not rows:
            return None  # нет даже строки session_metrics

        head = rows[0]
        bin_width_s = head["age_bin_width_s"]
        min_epochs_per_bin = head["age_bin_min_epochs"]
        if bin_width_s is None or min_epochs_per_bin is None:
            raise ValueError(
                f"metrics_id={metrics_id}: age_bin_width_s/age_bin_min_epochs "
                "не заполнены в session_metrics — для этой строки age-bin расчёт "
                "не выполнялся, серию восстановить нельзя."
            )

        # LEFT JOIN мог отдать одну строку с bin-полями = NULL (bins пусто).
        bin_rows = [r for r in rows if r["age_bin_start_s"] is not None]
        bins = tuple(
            AgeBinMetrics(
                age_bin_start_s=float(r["age_bin_start_s"]),
                age_bin_end_s=float(r["age_bin_end_s"]),
                epochs_count=int(r["epochs_count"]),
                hrms_m=float(r["hrms_m"]),
                vrms_m=float(r["vrms_m"]),
                cep50_m=float(r["cep50_m"]),
                r95_m=float(r["r95_m"]),
                is_significant=bool(r["is_significant"]),
            )
            for r in bin_rows
        )

        epochs_with_valid_age = sum(b.epochs_count for b in bins)

        return AgeBinMetricsSeries(
            session_id=int(head["session_id"]),
            stream_id=str(head["stream_id"]),
            solution_mode_filter=SolutionModeFilter(head["solution_mode_filter"]),
            bin_width_s=float(bin_width_s),
            min_epochs_per_bin=int(min_epochs_per_bin),
            epochs_after_filter=int(head["epochs_after_filter"]),
            epochs_rejected_outliers=int(head["epochs_rejected_outliers"]),
            epochs_with_valid_age=epochs_with_valid_age,
            bins=bins,
            computed_at=head["computed_at"],
        )
