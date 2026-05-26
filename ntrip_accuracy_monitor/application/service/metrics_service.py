"""Сервис расчёта метрик точности по сохранённому сеансу.

Тонкая обвязка над доменной функцией compute_metrics:
  1. читает SessionRow по session_id (SessionRepository);
  2. извлекает эталонную геодезическую точку из JSONB-поля
     reference_antenna;
  3. читает все эпохи указанного канала сеанса (EpochRepository
     с гарантией сортировки по epoch_time);
  4. прогоняет compute_metrics по всем четырём SolutionModeFilter
     (SPP, DGNSS, RTK_FIXED, RTK_FIXED_FLOAT) — фильтр без подходящих
     эпох автоматически опускается, так как compute_metrics возвращает
     None и эта None отфильтровывается здесь.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Final

from ntrip_accuracy_monitor.domain.metrics import (
    AccuracyMetrics,
    SolutionModeFilter,
    compute_metrics,
)
from ntrip_accuracy_monitor.domain.position import GeodeticPosition
from ntrip_accuracy_monitor.persistence.epoch_repository import EpochRepository
from ntrip_accuracy_monitor.persistence.session_repository import SessionRepository

_ALL_FILTERS: Final[Sequence[SolutionModeFilter]] = (
    SolutionModeFilter.SPP,
    SolutionModeFilter.DGNSS,
    SolutionModeFilter.RTK_FIXED,
    SolutionModeFilter.RTK_FIXED_FLOAT,
)

_DEFAULT_OUTLIER_FACTOR: Final[float] = 5.0


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

    Зависимости — два репозитория (sessions и epochs). Сервис никогда
    не открывает соединения сам и не управляет транзакциями: всё это
    делегировано репозиториям, которые работают через общий Executor
    (набор соединений к базе либо одно соединение).
    """

    def __init__(
        self,
        session_repository: SessionRepository,
        epoch_repository: EpochRepository,
    ) -> None:
        self._sessions = session_repository
        self._epochs = epoch_repository

    async def compute_session_metrics(
        self,
        session_id: int,
        stream_id: str,
        *,
        outlier_factor: float | None = _DEFAULT_OUTLIER_FACTOR,
    ) -> list[AccuracyMetrics]:
        """Рассчитать метрики для канала ``stream_id`` в сеансе ``session_id``.

        Возвращает по одному AccuracyMetrics на каждый SolutionModeFilter,
        для которого в выборке нашлась хотя бы одна подходящая эпоха.
        Порядок результатов соответствует _ALL_FILTERS.

        Если в указанном канале нет эпох — возвращает пустой список.

        Raises:
            ValueError: если сеанс не существует либо у сеанса нет
                ``reference_antenna`` (без эталона метрики не определены).
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

        return results
