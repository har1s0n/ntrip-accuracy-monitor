"""Подписчик RtcmHub: парсит RTCM-кадры и пишет метаданные в БД партиями.

По принципам и параметрам — копия EpochBatchWriter, отличия:

  1. Точка входа — consume_hub(queue): подписчик сам владеет циклом
     получения из asyncio.Queue, обрабатывает sentinel-None как конец
     потока. У EpochBatchWriter была submit(epoch) push-точка.
  2. Парсинг сырых байт через RtcmAdapter происходит внутри подписчика.
     Ошибки парса (RtcmParseError) логируются, счётчик parse_failures
     инкрементируется, обработка следующих фреймов продолжается.
  3. Дедупликации нет: у RTCM-кадров отсутствует естественный
     уникальный ключ (received_at нескольких кадров может совпасть
     при пакетной обработке, msg_type повторяется в потоке).

Поле satellite_id RtcmMessageRecord всегда выставляется в None — текущий
RtcmAdapter его не извлекает
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from typing import Final

import asyncpg

from ntrip_accuracy_monitor.persistence.rtcm_repository import (
    RtcmMessageRecord,
    RtcmRepository,
)
from ntrip_accuracy_monitor.protocols.ntrip._framer import stream_rtcm_frames
from ntrip_accuracy_monitor.protocols.rtcm.adapter import (
    RtcmAdapter,
    RtcmParseError,
)

logger: Final = logging.getLogger(__name__)

_TRANSIENT_DB_ERRORS: Final[tuple[type[BaseException], ...]] = (
    asyncpg.PostgresConnectionError,
    asyncpg.exceptions.ConnectionDoesNotExistError,
    asyncpg.exceptions.InterfaceError,
    OSError,
)


class _QueueByteReader:
    """AsyncByteReader поверх очереди подписки RtcmHub.

    RtcmHub кладёт в очередь куски сырых байт и sentinel-None в конце.
    Реализует протокол _framer.AsyncByteReader (метод read), чтобы подать
    поток в stream_rtcm_frames; sentinel-None → b"" (EOF для framer'а).
    """

    def __init__(self, queue: asyncio.Queue[bytes | None]) -> None:
        self._queue = queue
        self._buf = bytearray()
        self._eof = False

    async def read(self, n: int = -1) -> bytes:
        while not self._buf and not self._eof:
            item = await self._queue.get()
            if item is None:
                self._eof = True
            elif item:
                self._buf.extend(item)
        if n < 0 or n >= len(self._buf):
            out = bytes(self._buf)
            self._buf.clear()
            return out
        out = bytes(self._buf[:n])
        del self._buf[:n]
        return out


class RtcmAuditWriter:
    """Подписчик RtcmHub: парсит RTCM-кадры и пишет метаданные пачками."""

    def __init__(
        self,
        adapter: RtcmAdapter,
        repository: RtcmRepository,
        session_id_provider: Callable[[], int | None],
        *,
        flush_interval_s: float = 5.0,
        max_buffer_size: int = 500,
        retry_initial_backoff_s: float = 1.0,
        retry_max_backoff_s: float = 30.0,
        retry_total_timeout_s: float = 300.0,
    ) -> None:
        if flush_interval_s <= 0:
            raise ValueError(
                f"flush_interval_s must be > 0 (got {flush_interval_s})"
            )
        if max_buffer_size < 1:
            raise ValueError(
                f"max_buffer_size must be >= 1 (got {max_buffer_size})"
            )
        if retry_initial_backoff_s <= 0:
            raise ValueError(
                f"retry_initial_backoff_s must be > 0 "
                f"(got {retry_initial_backoff_s})"
            )
        if retry_max_backoff_s < retry_initial_backoff_s:
            raise ValueError(
                f"retry_max_backoff_s ({retry_max_backoff_s}) must be >= "
                f"retry_initial_backoff_s ({retry_initial_backoff_s})"
            )
        if retry_total_timeout_s <= 0:
            raise ValueError(
                f"retry_total_timeout_s must be > 0 "
                f"(got {retry_total_timeout_s})"
            )

        self._adapter = adapter
        self._repository = repository
        self._session_id_provider = session_id_provider
        self._flush_interval_s = flush_interval_s
        self._max_buffer_size = max_buffer_size
        self._retry_initial_backoff_s = retry_initial_backoff_s
        self._retry_max_backoff_s = retry_max_backoff_s
        self._retry_total_timeout_s = retry_total_timeout_s

        self._buffer: list[RtcmMessageRecord] = []
        self._flush_lock = asyncio.Lock()
        self._stop_requested = False

        self._frames_received = 0
        self._frames_parsed = 0
        self._parse_failures = 0
        self._dropped_no_session = 0
        self._dropped_db_unavailable = 0
        self._written_total = 0
        self._resync_bytes = 0

    # ----------------------------- properties -----------------------------
    @property
    def buffer_size(self) -> int:
        return len(self._buffer)

    @property
    def frames_received(self) -> int:
        return self._frames_received

    @property
    def frames_parsed(self) -> int:
        return self._frames_parsed

    @property
    def parse_failures(self) -> int:
        return self._parse_failures

    @property
    def dropped_no_session(self) -> int:
        return self._dropped_no_session

    @property
    def dropped_db_unavailable(self) -> int:
        return self._dropped_db_unavailable

    @property
    def written_total(self) -> int:
        return self._written_total

    @property
    def resync_bytes(self) -> int:
        return self._resync_bytes

    # ----------------------------- public API -----------------------------
    async def consume_hub(self, queue: asyncio.Queue[bytes | None]) -> None:
        """Главный цикл: выделяет RTCM3-кадры из сырого потока RtcmHub и пишет метаданные.

        RtcmHub отдаёт сырые байты (relay-поток, см. NtripClient(raw=True)),
        поэтому кадры выделяются здесь тем же stream_rtcm_frames, что и в
        транспорте. Не-RTCM3 байты (RTCM 2.x 6-of-8, мусор между кадрами,
        кадры с битым CRC) уходят в on_resync и в аудит не попадают —
        учитываются счётчиком resync_bytes.

        Завершается на:
          - sentinel-None из очереди → для _QueueByteReader это EOF, и
            итератор кадров останавливается;
          - asyncio.CancelledError (остановка снаружи).

        В обоих случаях в finally вызывается финальный flush. При cancellation
        финальный flush может быть прерван повторной отменой; это известное
        ограничение, документировано в module docstring.
        """
        reader = _QueueByteReader(queue)

        def on_resync(discarded: bytes) -> None:
            if discarded:
                self._resync_bytes += len(discarded)

        try:
            async for frame in stream_rtcm_frames(reader, on_resync=on_resync):
                self._frames_received += 1
                try:
                    msg = self._adapter.parse(frame)
                except RtcmParseError as exc:
                    self._parse_failures += 1
                    logger.warning(
                        "rtcm-audit: parse failed for %d-byte frame: %s",
                        len(frame), exc,
                    )
                    continue
                self._frames_parsed += 1
                self._buffer.append(
                    RtcmMessageRecord(
                        received_at=msg.received_at,
                        msg_type=msg.message_type,
                        reference_station_id=msg.station_id,
                        satellite_id=None,
                        byte_length=len(msg.raw),
                    )
                )
                if len(self._buffer) >= self._max_buffer_size:
                    await self.flush()
        finally:
            await self.flush()

    async def flush(self) -> None:
        """Атомарно забрать буфер и записать через RtcmRepository.insert_batch.

        Поведение под локом:
          - буфер пуст → no-op.
          - session_id is None → отбросить с инкрементом dropped_no_session.
          - insert_batch → экспоненциальный повтор на transient-ошибках,
            потолок retry_total_timeout_s; не-transient — re-raise.
        """
        async with self._flush_lock:
            if not self._buffer:
                return
            session_id = self._session_id_provider()
            if session_id is None:
                dropped = len(self._buffer)
                self._dropped_no_session += dropped
                logger.warning(
                    "rtcm-audit: no session_id, dropping %d buffered records",
                    dropped,
                )
                self._buffer.clear()
                return

            batch = self._buffer
            self._buffer = []
            await self._write_with_retry(session_id, batch)

    async def run_background_flusher(self) -> None:
        """Периодический сброс буфера. Завершается на stop() или cancel.

        В finally делает финальный flush на случай, если за последний
        sleep_interval_s буфер успел накопиться.
        """
        try:
            while not self._stop_requested:
                await asyncio.sleep(self._flush_interval_s)
                if self._buffer:
                    await self.flush()
        finally:
            await self.flush()

    def stop(self) -> None:
        """Сигнал для run_background_flusher завершиться на следующем тике."""
        self._stop_requested = True

    # ---------------------------- internals -------------------------------
    async def _write_with_retry(
        self,
        session_id: int,
        batch: list[RtcmMessageRecord],
    ) -> None:
        deadline = time.monotonic() + self._retry_total_timeout_s
        backoff = self._retry_initial_backoff_s
        attempt = 0
        while True:
            attempt += 1
            try:
                await self._repository.insert_batch(session_id, batch)
                self._written_total += len(batch)
                logger.debug(
                    "rtcm-audit: wrote %d records (attempt %d)",
                    len(batch), attempt,
                )
                return
            except _TRANSIENT_DB_ERRORS as exc:
                now = time.monotonic()
                if now >= deadline:
                    self._dropped_db_unavailable += len(batch)
                    logger.error(
                        "rtcm-audit: db unavailable after %d attempts, "
                        "dropping %d records: %s",
                        attempt, len(batch), exc,
                    )
                    return
                sleep_s = min(backoff, deadline - now)
                logger.warning(
                    "rtcm-audit: db write failed (attempt %d), "
                    "retry in %.2fs: %s",
                    attempt, sleep_s, exc,
                )
                await asyncio.sleep(sleep_s)
                backoff = min(backoff * 2, self._retry_max_backoff_s)
