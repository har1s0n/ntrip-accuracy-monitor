"""Пакетная запись Epoch в БД через EpochRepository.

Стратегия сброса буфера — что наступит раньше: по таймеру или по размеру.
Дедупликация по ключу (stream_id, epoch_time) — всегда перед вставкой.
При потере связи с БД — экспоненциальная пауза до общего потолка
retry_total_timeout_s, после чего партия отбрасывается со счётчиком.

Снимок буфера для записи делается под asyncio.Lock, сама вставка
с повторами — вне блокировки, чтобы submit не упирался в долгий повтор.
"""

from __future__ import annotations

import asyncio
import logging
import time as time_module
from collections.abc import Callable
from datetime import datetime
from typing import Final

import asyncpg

from ntrip_accuracy_monitor.domain.epoch import Epoch
from ntrip_accuracy_monitor.persistence.epoch_repository import EpochRepository

_LOG: Final = logging.getLogger(__name__)

# Исключения, которые трактуем как «временно недоступна БД».
_TRANSIENT_DB_ERRORS: Final[tuple[type[BaseException], ...]] = (
    asyncpg.PostgresConnectionError,
    asyncpg.ConnectionDoesNotExistError,
    asyncpg.InterfaceError,
    ConnectionError,
    asyncio.TimeoutError,
    OSError,
)


class EpochBatchWriter:
    """Буферизирует Epoch и пишет партиями в EpochRepository.

    Сброс инициирует:
      - submit(), если len(buffer) >= max_buffer_size;
      - run_background_flusher() по таймеру flush_interval_s;
      - flush() — публичный ручной триггер.

    Если session_id_provider возвращает None — копим до max_buffer_size,
    дальше отбрасываем устаревшие со счётчиком dropped_no_session.
    """

    def __init__(
        self,
        repository: EpochRepository,
        session_id_provider: Callable[[], int | None],
        *,
        flush_interval_s: float = 5.0,
        max_buffer_size: int = 500,
        retry_initial_backoff_s: float = 1.0,
        retry_max_backoff_s: float = 30.0,
        retry_total_timeout_s: float = 300.0,
    ) -> None:
        if flush_interval_s <= 0:
            raise ValueError("flush_interval_s должен быть > 0")
        if max_buffer_size <= 0:
            raise ValueError("max_buffer_size должен быть > 0")
        if retry_initial_backoff_s <= 0:
            raise ValueError("retry_initial_backoff_s должен быть > 0")
        if retry_max_backoff_s < retry_initial_backoff_s:
            raise ValueError(
                "retry_max_backoff_s должен быть >= retry_initial_backoff_s"
            )

        self._repo = repository
        self._session_id_provider = session_id_provider
        self._flush_interval_s = flush_interval_s
        self._max_buffer_size = max_buffer_size
        self._retry_initial_backoff_s = retry_initial_backoff_s
        self._retry_max_backoff_s = retry_max_backoff_s
        self._retry_total_timeout_s = retry_total_timeout_s

        self._buffer: list[Epoch] = []
        self._snapshot_lock = asyncio.Lock()
        self._stop_event = asyncio.Event()
        self._no_session_warned = False

        # Счётчики.
        self.dropped_no_session: int = 0
        self.dropped_db_unavailable: int = 0
        self.written_total: int = 0

    @property
    def buffer_size(self) -> int:
        return len(self._buffer)

    async def submit(self, epoch: Epoch) -> None:
        """Добавить эпоху в буфер. Может триггерить flush по размеру."""
        snapshot: list[Epoch] | None = None
        session_for_flush: int | None = None

        async with self._snapshot_lock:
            self._buffer.append(epoch)
            session_id = self._session_id_provider()
            if session_id is None:
                self._enforce_no_session_cap_locked()
                return
            if len(self._buffer) >= self._max_buffer_size:
                snapshot = self._buffer
                self._buffer = []
                session_for_flush = session_id

        if snapshot is not None and session_for_flush is not None:
            await self._write_with_retry(session_for_flush, snapshot)

    async def flush(self) -> None:
        """Принудительно сбросить буфер в БД."""
        async with self._snapshot_lock:
            session_id = self._session_id_provider()
            if session_id is None or not self._buffer:
                return
            snapshot = self._buffer
            self._buffer = []
        await self._write_with_retry(session_id, snapshot)

    async def run_background_flusher(self) -> None:
        """Фоновая задача периодического сброса по таймеру.

        Останавливается через stop(). Исключения внутри _write_with_retry
        не пробрасываются — иначе задача упадёт и таймер остановится.
        """
        _LOG.info(
            "EpochBatchWriter: фоновый сброс каждые %.1f с",
            self._flush_interval_s,
        )
        while not self._stop_event.is_set():
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(),
                    timeout=self._flush_interval_s,
                )
                break  # stop_event сработал
            except asyncio.TimeoutError:
                pass
            try:
                await self.flush()
            except Exception:
                # _write_with_retry свои ошибки уже логирует и подавляет.
                _LOG.exception("EpochBatchWriter: фоновый сброс упал")

    def stop(self) -> None:
        """Сигнал фоновой задаче на завершение. Один последний flush —
        делается отдельным вызовом await writer.flush() после stop()."""
        self._stop_event.set()

    def _enforce_no_session_cap_locked(self) -> None:
        """Под self._snapshot_lock. Обрезает буфер до max_buffer_size."""
        excess = len(self._buffer) - self._max_buffer_size
        if excess <= 0:
            return
        del self._buffer[:excess]
        self.dropped_no_session += excess
        if not self._no_session_warned:
            _LOG.warning(
                "EpochBatchWriter: нет активного сеанса, буфер заполнен — "
                "отбрасываем устаревшие эпохи (счётчик растёт)"
            )
            self._no_session_warned = True

    async def _write_with_retry(
        self,
        session_id: int,
        snapshot: list[Epoch],
    ) -> None:
        """Записать снимок с дедупликацией и повторами при временных сбоях."""
        deduped = self._dedupe(snapshot)
        if not deduped:
            return

        backoff = self._retry_initial_backoff_s
        started = time_module.monotonic()
        while True:
            try:
                await self._repo.insert_batch(session_id, deduped)
                self.written_total += len(deduped)
                return
            except asyncpg.UniqueViolationError:
                # После дедупликации в одном процессе попасть сюда нельзя.
                # Если попали — другой поток вставил параллельно.
                _LOG.error(
                    "EpochBatchWriter: UniqueViolationError после "
                    "дедупликации, отбрасываем партию (%d эпох)",
                    len(deduped),
                )
                self.dropped_db_unavailable += len(deduped)
                return
            except _TRANSIENT_DB_ERRORS as exc:
                elapsed = time_module.monotonic() - started
                if elapsed >= self._retry_total_timeout_s:
                    _LOG.error(
                        "EpochBatchWriter: БД недоступна %.0f с, "
                        "отбрасываем %d эпох",
                        elapsed,
                        len(deduped),
                    )
                    self.dropped_db_unavailable += len(deduped)
                    return
                _LOG.warning(
                    "EpochBatchWriter: вставка упала (%s: %s), "
                    "повтор через %.1f с",
                    type(exc).__name__,
                    exc,
                    backoff,
                )
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2.0, self._retry_max_backoff_s)

    @staticmethod
    def _dedupe(snapshot: list[Epoch]) -> list[Epoch]:
        """Дедуплицировать по (stream_id, epoch_time), оставляя последнюю."""
        # dict сохраняет порядок вставки; при коллизии перезаписывается.
        deduped: dict[tuple[str, datetime], Epoch] = {}
        for ep in snapshot:
            deduped[(ep.stream_id, ep.epoch_time)] = ep
        return list(deduped.values())
