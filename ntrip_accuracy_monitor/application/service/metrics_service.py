"""Сервис расчёта метрик точности по сохранённому сеансу.

Тонкая обвязка над доменными функциями compute_metrics и
compute_age_bin_metrics:
  1. читает SessionRow по session_id (SessionRepository);
  2. извлекает эталонную геодезическую точку из JSONB-поля
     reference_antenna;
  3. читает все эпохи указанного канала сеанса (EpochRepository
     с гарантией сортировки по epoch_time);
  4. прогоняет доменную функцию по всем четырём SolutionModeFilter
     (SPP, DGNSS, RTK_FIXED, RTK_FIXED_FLOAT) — фильтр без подходящих
     эпох автоматически опускается, так как доменная функция возвращает
     None и эта None отфильтровывается здесь.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Final

from ntrip_accuracy_monitor.domain.age_bins import (
    AgeBinMetricsSeries,
    compute_age_bin_metrics,
)
from ntrip_accuracy_monitor.domain.metrics import (
    AccuracyMetrics,
    SolutionModeFilter,
    compute_metrics,
)
from ntrip_accuracy_monitor.domain.position import GeodeticPosition
from ntrip_accuracy_monitor.persistence._executor import (
    Executor,
    acquire_connection,
)
from ntrip_accuracy_monitor.persistence.age_bin_metrics_repository import (
    AgeBinMetricsRepository,
)
from ntrip_accuracy_monitor.persistence.epoch_repository import EpochRepository
from ntrip_accuracy_monitor.persistence.metrics_repository import (
    MetricsRepository,
)
from ntrip_accuracy_monitor.persistence.session_repository import (
    SessionRepository,
)

_ALL_FILTERS: Final[Sequence[SolutionModeFilter]] = (
    SolutionModeFilter.SPP,
    SolutionModeFilter.DGNSS,
    SolutionModeFilter.RTK_FIXED,
    SolutionModeFilter.RTK_FIXED_FLOAT,
)

_DEFAULT_OUTLIER_FACTOR: Final[float] = 5.0
_DEFAULT_BIN_WIDTH_S: Final[float] = 1.0
_DEFAULT_MIN_EPOCHS_PER_BIN: Final[int] = 30


def _extract_reference(
    reference_antenna: dict[str, Any] | None,
    session_id: int,
) -> GeodeticPosition:
    """Достать lat/lon/h из JSONB-поля sessions.reference_antenna."""
    if reference_antenna is None:
        raise ValueError(
            f"session {session_id} has no reference_antenna; "
            "cannot compute accuracy metrics without a known reference point"
        )
    try:
        return GeodeticPosition(
            latitude_deg=float(reference_antenna["latitude_deg"]),
            longitude_deg=float(reference_antenna["longitude_deg"]),
            ellipsoidal_height_m=float(reference_antenna["ellipsoidal_height_m"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(
            f"session {session_id} reference_antenna is malformed: {exc}"
        ) from exc


class MetricsService:
    """Расчёт метрик точности для одного канала одного сеанса.

    Обязательные зависимости — два репозитория (sessions и epochs).
    Опциональные (для persist=True) — executor + MetricsRepository +
    AgeBinMetricsRepository. Без них persist=True поднимает
    RuntimeError; persist=False работает всегда.
    """

    def __init__(
        self,
        session_repository: SessionRepository,
        epoch_repository: EpochRepository,
        *,
        executor: Executor | None = None,
        metrics_repository: MetricsRepository | None = None,
        age_bin_metrics_repository: AgeBinMetricsRepository | None = None,
    ) -> None:
        self._sessions = session_repository
        self._epochs = epoch_repository
        # Тройка persistence-зависимостей — опциональна. None означает,
        # что сервис сконфигурирован только под расчёт (persist=False).
        # При persist=True все три обязаны быть заданы — см.
        # _require_persistence.
        self._executor = executor
        self._metrics_repo = metrics_repository
        self._age_bin_repo = age_bin_metrics_repository

    def _require_persistence(
        self,
    ) -> tuple[Executor, MetricsRepository, AgeBinMetricsRepository]:
        """Проверить, что persist=True допустим в текущей конфигурации.

        Возвращает кортеж не-None зависимостей, чтобы mypy в дальнейшем
        видел их без проверки на None. При отсутствии любого из них —
        RuntimeError с понятной подсказкой.
        """
        if (
            self._executor is None
            or self._metrics_repo is None
            or self._age_bin_repo is None
        ):
            raise RuntimeError(
                "persist=True требует executor, metrics_repository и "
                "age_bin_metrics_repository в MetricsService.__init__. "
                "Для расчёта без записи в БД используйте persist=False."
            )
        return self._executor, self._metrics_repo, self._age_bin_repo

    async def compute_session_metrics(
        self,
        session_id: int,
        stream_id: str,
        *,
        outlier_factor: float | None = _DEFAULT_OUTLIER_FACTOR,
        persist: bool = False,
    ) -> list[AccuracyMetrics]:
        """Рассчитать метрики для канала ``stream_id`` в сеансе ``session_id``.

        Возвращает по одному AccuracyMetrics на каждый SolutionModeFilter,
        для которого в выборке нашлась хотя бы одна подходящая эпоха.
        Порядок результатов соответствует _ALL_FILTERS.

        Если в указанном канале нет эпох — возвращает пустой список.

        persist: если True, после расчёта каждая AccuracyMetrics из
            результата записывается в session_metrics через
            MetricsRepository.upsert в одной транзакции. Параметры
            биннинга (age_bin_width_s, age_bin_min_epochs) передаются
            None — существующие значения в БД сохраняются благодаря
            COALESCE в _UPSERT_SQL (см. чат №11.1, Q8). Требует, чтобы
            сервис был сконфигурирован с executor + metrics_repository +
            age_bin_metrics_repository в __init__.

        Raises:
            ValueError: если сеанс не существует либо у сеанса нет
                ``reference_antenna`` (без эталона метрики не определены).
            RuntimeError: если persist=True, но persistence-зависимости
                не сконфигурированы.
        """
        session = await self._sessions.get_by_id(session_id)
        if session is None:
            raise ValueError(f"session {session_id} not found")

        reference = _extract_reference(session.reference_antenna, session_id)

        epochs = await self._epochs.fetch_for_session_stream(session_id, stream_id)
        if not epochs:
            return []

        results: list[AccuracyMetrics] = []
        for solution_filter in _ALL_FILTERS:
            metrics = compute_metrics(
                epochs,
                reference,
                session_id=session_id,
                stream_id=stream_id,
                solution_mode_filter=solution_filter,
                outlier_factor=outlier_factor,
            )
            if metrics is not None:
                results.append(metrics)

        if persist and results:
            executor, metrics_repo, _ = self._require_persistence()
            async with acquire_connection(executor) as conn:
                async with conn.transaction():
                    for metrics in results:
                        await metrics_repo.upsert(
                            metrics,
                            outlier_factor=outlier_factor,
                            # age_bin_*=None — COALESCE в _UPSERT_SQL
                            # сохранит существующие значения, не затрёт.
                            conn=conn,
                        )

        return results

    async def compute_session_age_bin_metrics(
        self,
        session_id: int,
        stream_id: str,
        *,
        bin_width_s: float = _DEFAULT_BIN_WIDTH_S,
        min_epochs_per_bin: int = _DEFAULT_MIN_EPOCHS_PER_BIN,
        outlier_factor: float | None = _DEFAULT_OUTLIER_FACTOR,
        persist: bool = False,
    ) -> list[AgeBinMetricsSeries]:
        """Рассчитать биннинг метрик по age_of_corrections_s.

        Возвращает по одной AgeBinMetricsSeries на каждый
        SolutionModeFilter, для которого compute_age_bin_metrics
        смог сформировать хотя бы один бин. SPP отсеивается внутри
        compute_age_bin_metrics (age для SPP всегда None) и в
        результат не попадает.

        Порядок результатов соответствует _ALL_FILTERS (SPP, DGNSS,
        RTK_FIXED, RTK_FIXED_FLOAT) минус опущенные None.

        Если в указанном канале нет эпох — возвращает пустой список.

        persist: если True, для каждой возвращённой AgeBinMetricsSeries
            дополнительно рассчитывается AccuracyMetrics на тех же
            эпохах (дубль расчёта ради атомарности — см. чат №11.1, Q7),
            записывается в session_metrics через upsert с заполненными
            age_bin_width_s / age_bin_min_epochs, и тут же в
            metrics_by_age заливаются bins через
            AgeBinMetricsRepository.insert_series. Всё это в одной
            транзакции. SPP в этом методе не участвует — для записи
            метрик SPP используйте compute_session_metrics(persist=True).

        Raises:
            ValueError: если сеанс не существует, либо у сеанса нет
                ``reference_antenna``, либо параметры биннинга
                невалидны (валидацию делает compute_age_bin_metrics).
            RuntimeError: если persist=True, но persistence-зависимости
                не сконфигурированы; либо если в ходе persist
                compute_metrics неожиданно вернул None для фильтра,
                по которому AgeBinMetricsSeries был получен
                (рассогласование выборок).
        """
        session = await self._sessions.get_by_id(session_id)
        if session is None:
            raise ValueError(f"session {session_id} not found")

        reference = _extract_reference(session.reference_antenna, session_id)

        epochs = await self._epochs.fetch_for_session_stream(session_id, stream_id)
        if not epochs:
            return []

        results: list[AgeBinMetricsSeries] = []
        for solution_filter in _ALL_FILTERS:
            series = compute_age_bin_metrics(
                epochs,
                reference,
                session_id=session_id,
                stream_id=stream_id,
                solution_mode_filter=solution_filter,
                bin_width_s=bin_width_s,
                min_epochs_per_bin=min_epochs_per_bin,
                outlier_factor=outlier_factor,
            )
            if series is not None:
                results.append(series)

        if persist and results:
            executor, metrics_repo, age_bin_repo = self._require_persistence()
            async with acquire_connection(executor) as conn:
                async with conn.transaction():
                    for series in results:
                        # Inline-расчёт AccuracyMetrics для того же
                        # фильтра на тех же эпохах. Дубль работы — цена
                        # за атомарность session_metrics + metrics_by_age
                        # (см. чат №11.1, Q7).
                        metrics = compute_metrics(
                            epochs,
                            reference,
                            session_id=session_id,
                            stream_id=stream_id,
                            solution_mode_filter=series.solution_mode_filter,
                            outlier_factor=outlier_factor,
                            computed_at=series.computed_at,
                        )
                        if metrics is None:
                            raise RuntimeError(
                                f"AgeBinMetricsSeries для "
                                f"{series.solution_mode_filter.value} получен, "
                                f"но compute_metrics вернул None — "
                                f"рассогласование выборок compute_age_bin_metrics "
                                f"и compute_metrics."
                            )
                        metrics_id = await metrics_repo.upsert(
                            metrics,
                            outlier_factor=outlier_factor,
                            age_bin_width_s=series.bin_width_s,
                            age_bin_min_epochs=series.min_epochs_per_bin,
                            conn=conn,
                        )
                        await age_bin_repo.insert_series(
                            series, metrics_id, conn=conn,
                        )

        return results
