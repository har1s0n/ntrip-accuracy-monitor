"""TCP-транспорт NMEA-0183 c line framing, watchdog'ом и авто-реконнектом.

Слой между сокетом приёмника EFT RS3 и парсером (`protocols.nmea.parser`).
Один экземпляр транспорта = один stream_id = один приёмник. Отдаёт уже
распарсенные доменные record-ы; невалидные строки логирует и пропускает,
не прерывая поток.
"""

from __future__ import annotations

import asyncio
import logging
import random
from asyncio import IncompleteReadError, LimitOverrunError, StreamReader, StreamWriter
from collections.abc import AsyncIterator
from contextlib import suppress
from datetime import UTC, datetime
from types import TracebackType
from typing import Self

from ntrip_accuracy_monitor.protocols.backoff import BackoffPolicy
from ntrip_accuracy_monitor.protocols.nmea.errors import NmeaChecksumError, NmeaError
from ntrip_accuracy_monitor.protocols.nmea.messages import NmeaRecord
from ntrip_accuracy_monitor.protocols.nmea.parser import parse_line

_LOGGER_NAME = "ntrip_accuracy_monitor.protocols.nmea.transport"

# Стандарт NMEA-0183 ограничивает строку 82 байтами; берём запас 256.
MAX_NMEA_LINE_LEN = 256

# Логи non-NMEA байт rate-limited: первые N случаев — INFO, дальше только счётчик.
_NON_NMEA_LOG_LIMIT = 3
# Шаг логирования счётчика parse-ошибок.
_PARSE_ERROR_LOG_EVERY = 100


class _StreamReset(Exception):
    """Внутренний сигнал: текущее соединение мертво, требуется реконнект."""


class NmeaTcpClient:
    """Async-итератор по потоку `NmeaRecord` из TCP-сокета приемника.

    Lifecycle через `async with`. Поток отдаёт уже распарсенные record-ы;
    битые строки пропускаются с инкрементом счетчика. Сетевые сбои и stall
    (нет данных дольше `stall_timeout_s`) приводят к закрытию сокета и
    реконнекту с экспоненциальным backoff'ом.

    Счетчик попыток сбрасывается только после получения первой валидной
    NMEA-строки на новом коннекте — это защищает от флапа (мгновенный
    коннект → мгновенный разрыв без полезных данных).
    """

    def     __init__(
        self,
        *,
        stream_id: str,
        host: str,
        port: int,
        connect_timeout_s: float,
        stall_timeout_s: float,
        backoff: BackoffPolicy,
        rng: random.Random | None = None,
    ) -> None:
        self._stream_id = stream_id
        self._host = host
        self._port = port
        self._connect_timeout_s = connect_timeout_s
        self._stall_timeout_s = stall_timeout_s
        self._backoff = backoff
        self._rng = rng

        self._reader: StreamReader | None = None
        self._writer: StreamWriter | None = None
        self._closed = False
        self._connect_attempt = 0
        self._first_valid_received = False

        # Публичные счётчики (read-only через property).
        self._parse_errors = 0
        self._checksum_failures = 0
        self._non_nmea_lines = 0
        self._reconnects = 0
        self._non_nmea_log_count = 0

        base_logger = logging.getLogger(_LOGGER_NAME)
        self._log = logging.LoggerAdapter(base_logger, {"stream_id": stream_id})

    # ---- Счётчики (для оркестратора и тестов) ------------------------------

    @property
    def parse_errors(self) -> int:
        return self._parse_errors

    @property
    def checksum_failures(self) -> int:
        return self._checksum_failures

    @property
    def non_nmea_lines(self) -> int:
        return self._non_nmea_lines

    @property
    def reconnects(self) -> int:
        return self._reconnects

    # ---- Контекст-менеджер и итератор --------------------------------------
    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()

    def __aiter__(self) -> AsyncIterator[NmeaRecord]:
        return self

    async def __anext__(self) -> NmeaRecord:
        while True:
            if self._closed:
                raise StopAsyncIteration
            try:
                if self._reader is None:
                    await self._connect_with_backoff()
                    if self._reader is None:
                        # closed произошёл во время backoff
                        raise StopAsyncIteration
                line = await self._read_line()
                record = self._try_parse(line)
                if record is not None:
                    return record
            except _StreamReset:
                # Соединение мертво, _reader=None, цикл сделает reconnect.
                continue

    async def aclose(self) -> None:
        """Закрыть транспорт. После вызова итератор вернет StopAsyncIteration."""
        self._closed = True
        await self._reset_connection()

    # ---- Реконнект ---------------------------------------------------------

    async def _connect_with_backoff(self) -> None:
        """Установить соединение, ретраясь по политике backoff'а до успеха.

        Не перехватывает `CancelledError` - отмена снаружи завершает попытку.
        """
        while not self._closed:
            if self._connect_attempt > 0:
                delay = self._backoff.delay_for_attempt(
                    self._connect_attempt - 1, rng=self._rng,
                )
                self._log.warning(
                    "reconnecting in %.2fs (attempt %d)",
                    delay, self._connect_attempt + 1,
                )
                await asyncio.sleep(delay)
            self._connect_attempt += 1
            try:
                async with asyncio.timeout(self._connect_timeout_s):
                    self._reader, self._writer = await asyncio.open_connection(
                        self._host, self._port,
                    )
            except (OSError, TimeoutError) as exc:
                self._log.warning(
                    "connect failed (attempt %d): %s",
                    self._connect_attempt, exc,
                )
                continue
            self._log.info(
                "connected to %s:%d on attempt %d",
                self._host, self._port, self._connect_attempt,
            )
            return

    async def _reset_connection(self) -> None:
        """Закрыть сокет, обнулить state коннекта (idempotent)."""
        writer = self._writer
        self._reader = None
        self._writer = None
        self._first_valid_received = False
        if writer is None:
            return
        with suppress(OSError):
            writer.close()
            await writer.wait_closed()

    async def _on_connection_lost(self) -> None:
        self._reconnects += 1
        await self._reset_connection()

    # ---- Чтение и парсинг --------------------------------------------------
    async def _read_line(self) -> bytes:
        """Прочитать одну строку до `\\n`. На сетевых ошибках бросает _StreamReset."""
        reader = self._reader
        if reader is None:
            raise _StreamReset
        try:
            async with asyncio.timeout(self._stall_timeout_s):
                line = await reader.readuntil(b"\n")
        except TimeoutError:
            self._log.warning(
                "stall timeout (%.1fs), reconnecting", self._stall_timeout_s,
            )
            await self._on_connection_lost()
            raise _StreamReset from None
        except IncompleteReadError:
            self._log.info("server closed connection, reconnecting")
            await self._on_connection_lost()
            raise _StreamReset from None
        except LimitOverrunError:
            self._log.warning("read buffer overrun, reconnecting")
            await self._on_connection_lost()
            raise _StreamReset from None
        except OSError as exc:
            self._log.warning("read failed: %s, reconnecting", exc)
            await self._on_connection_lost()
            raise _StreamReset from None
        return line

    def _try_parse(self, raw: bytes) -> NmeaRecord | None:
        """Прогнать сырую строку через фильтры → парсер. None — пропустить."""
        line = raw.rstrip(b"\r\n")
        if not line:
            return None
        if not line.startswith(b"$"):
            self._non_nmea_lines += 1
            self._maybe_log_non_nmea(line)
            return None
        if len(line) > MAX_NMEA_LINE_LEN:
            self._log.warning(
                "line exceeds max length %d, dropped (got %d)",
                MAX_NMEA_LINE_LEN, len(line),
            )
            return None
        # TODO(): реконсиляция даты через RMC/ZDA для устранения
        # ±1-секундного окна ошибки на границе суток UTC.
        today_utc = datetime.now(UTC).date()
        try:
            record = parse_line(line, today_utc=today_utc)
        except NmeaChecksumError:
            self._checksum_failures += 1
            return None
        except NmeaError:
            self._parse_errors += 1
            if self._parse_errors % _PARSE_ERROR_LOG_EVERY == 0:
                self._log.warning("parse errors total: %d", self._parse_errors)
            return None
        if record is None:
            return None
        if not self._first_valid_received:
            self._first_valid_received = True
            self._connect_attempt = 0
            self._log.info("first valid NMEA record received")
        return record

    def _maybe_log_non_nmea(self, line: bytes) -> None:
        if self._non_nmea_log_count < _NON_NMEA_LOG_LIMIT:
            self._non_nmea_log_count += 1
            self._log.info(
                "non-NMEA line dropped: %r (total=%d)",
                line[:32], self._non_nmea_lines,
            )
