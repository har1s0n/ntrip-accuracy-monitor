"""Тесты RtcmAuditWriter: парсинг, буферизация, повторы, остановка."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Final
from unittest.mock import AsyncMock, MagicMock

import asyncpg
import pytest

from ntrip_accuracy_monitor.application.audit.rtcm_audit_writer import (
    RtcmAuditWriter,
)
from ntrip_accuracy_monitor.protocols.rtcm.adapter import (
    RtcmMessage,
    RtcmParseError,
)
from ntrip_accuracy_monitor.protocols.ntrip._framer import crc24q

_SESSION_ID: Final[int] = 42


def _make_frame(msg_type: int = 1004, body: bytes = b"\x00" * 20) -> bytes:
    """Минимальный CRC-валидный RTCM3-кадр."""
    payload = bytes([(msg_type >> 4) & 0xFF, (msg_type & 0x0F) << 4]) + body
    length = len(payload)
    head = bytes([0xD3, (length >> 8) & 0x03, length & 0xFF])
    frame = head + payload
    crc = crc24q(frame)
    return frame + bytes([(crc >> 16) & 0xFF, (crc >> 8) & 0xFF, crc & 0xFF])


def _make_rtcm_message(
    *,
    raw: bytes = b"\xd3\x00\x01\xff" + b"\x00" * 20,
    msg_type: int = 1004,
    station_id: int | None = 1234,
) -> RtcmMessage:
    return RtcmMessage(
        raw=raw,
        message_type=msg_type,
        received_at=datetime(2025, 1, 1, 12, 0, 0, tzinfo=UTC),
        station_id=station_id,
        epoch_time_ms=None,
    )


@pytest.fixture
def adapter() -> MagicMock:
    a = MagicMock()
    a.parse.return_value = _make_rtcm_message()
    return a


@pytest.fixture
def repository() -> AsyncMock:
    r = AsyncMock()
    r.insert_batch = AsyncMock(return_value=None)
    return r


def _make_writer(
    adapter: MagicMock,
    repository: AsyncMock,
    *,
    session_id: int | None = _SESSION_ID,
    flush_interval_s: float = 60.0,  # большой по умолчанию — фоновый не мешает
    max_buffer_size: int = 500,
    retry_initial_backoff_s: float = 0.01,
    retry_max_backoff_s: float = 0.01,
    retry_total_timeout_s: float = 0.5,
) -> RtcmAuditWriter:
    return RtcmAuditWriter(
        adapter=adapter,
        repository=repository,
        session_id_provider=lambda: session_id,
        flush_interval_s=flush_interval_s,
        max_buffer_size=max_buffer_size,
        retry_initial_backoff_s=retry_initial_backoff_s,
        retry_max_backoff_s=retry_max_backoff_s,
        retry_total_timeout_s=retry_total_timeout_s,
    )


# --------------------------------------------------------------------------
# Параметры и инициализация
# --------------------------------------------------------------------------
def test_init_rejects_invalid_parameters(
    adapter: MagicMock, repository: AsyncMock,
) -> None:
    with pytest.raises(ValueError, match="flush_interval_s"):
        RtcmAuditWriter(adapter, repository, lambda: 1, flush_interval_s=0.0)
    with pytest.raises(ValueError, match="max_buffer_size"):
        RtcmAuditWriter(adapter, repository, lambda: 1, max_buffer_size=0)
    with pytest.raises(ValueError, match="retry_max_backoff_s"):
        RtcmAuditWriter(
            adapter, repository, lambda: 1,
            retry_initial_backoff_s=10.0, retry_max_backoff_s=1.0,
        )


# --------------------------------------------------------------------------
# consume_hub: парсинг и буферизация
# --------------------------------------------------------------------------
async def test_consume_hub_parses_and_buffers_below_threshold(
    adapter: MagicMock, repository: AsyncMock,
) -> None:
    writer = _make_writer(adapter, repository, max_buffer_size=10)
    q: asyncio.Queue[bytes | None] = asyncio.Queue()
    for _ in range(3):
        await q.put(_make_frame())
    await q.put(None)  # завершение

    await writer.consume_hub(q)

    assert writer.frames_received == 3
    assert writer.frames_parsed == 3
    assert writer.parse_failures == 0
    # Финальный flush в finally записал 3 записи одной партией.
    assert writer.written_total == 3
    repository.insert_batch.assert_awaited_once()


async def test_consume_hub_full_buffer_triggers_intermediate_flush(
    adapter: MagicMock, repository: AsyncMock,
) -> None:
    writer = _make_writer(adapter, repository, max_buffer_size=2)
    q: asyncio.Queue[bytes | None] = asyncio.Queue()
    for _ in range(5):
        await q.put(_make_frame())
    await q.put(None)

    await writer.consume_hub(q)

    # 5 фреймов при max_buffer_size=2 → flush после 2-го и 4-го,
    # затем финальный flush с последним 5-м. Итого 3 партии.
    assert writer.written_total == 5
    assert repository.insert_batch.await_count == 3


async def test_consume_hub_sentinel_triggers_final_flush(
    adapter: MagicMock, repository: AsyncMock,
) -> None:
    writer = _make_writer(adapter, repository, max_buffer_size=100)
    q: asyncio.Queue[bytes | None] = asyncio.Queue()
    await q.put(_make_frame())
    await q.put(_make_frame())
    await q.put(None)

    await writer.consume_hub(q)

    assert writer.written_total == 2
    repository.insert_batch.assert_awaited_once()


async def test_consume_hub_skips_parse_errors_and_continues(
    repository: AsyncMock,
) -> None:
    adapter = MagicMock()
    adapter.parse.side_effect = [
        _make_rtcm_message(msg_type=1004),
        RtcmParseError("bad frame"),
        _make_rtcm_message(msg_type=1019),
    ]
    writer = _make_writer(adapter, repository)
    q: asyncio.Queue[bytes | None] = asyncio.Queue()
    for _ in range(3):
        await q.put(_make_frame())
    await q.put(None)

    await writer.consume_hub(q)

    assert writer.frames_received == 3
    assert writer.frames_parsed == 2
    assert writer.parse_failures == 1
    assert writer.written_total == 2


# --------------------------------------------------------------------------
# flush: session_id и БД
# --------------------------------------------------------------------------
async def test_flush_without_session_id_drops_buffer(
    adapter: MagicMock, repository: AsyncMock,
) -> None:
    writer = _make_writer(adapter, repository, session_id=None)
    q: asyncio.Queue[bytes | None] = asyncio.Queue()
    await q.put(_make_frame())
    await q.put(_make_frame())
    await q.put(None)

    await writer.consume_hub(q)

    assert writer.dropped_no_session == 2
    assert writer.written_total == 0
    repository.insert_batch.assert_not_awaited()


async def test_flush_transient_error_retries_then_succeeds(
    adapter: MagicMock,
) -> None:
    repository = AsyncMock()
    repository.insert_batch = AsyncMock(
        side_effect=[OSError("connection lost"), None],
    )
    writer = _make_writer(adapter, repository)
    q: asyncio.Queue[bytes | None] = asyncio.Queue()
    await q.put(_make_frame())
    await q.put(None)

    await writer.consume_hub(q)

    assert writer.written_total == 1
    assert writer.dropped_db_unavailable == 0
    assert repository.insert_batch.await_count == 2


async def test_flush_persistent_db_error_drops_batch_after_timeout(
    adapter: MagicMock,
) -> None:
    repository = AsyncMock()
    repository.insert_batch = AsyncMock(
        side_effect=asyncpg.PostgresConnectionError("db down"),
    )
    writer = _make_writer(
        adapter, repository,
        retry_initial_backoff_s=0.01,
        retry_max_backoff_s=0.01,
        retry_total_timeout_s=0.05,
    )
    q: asyncio.Queue[bytes | None] = asyncio.Queue()
    await q.put(_make_frame())
    await q.put(_make_frame())
    await q.put(None)

    await writer.consume_hub(q)

    assert writer.written_total == 0
    assert writer.dropped_db_unavailable == 2


# --------------------------------------------------------------------------
# Фоновый таймер и stop()
# --------------------------------------------------------------------------
async def test_background_flusher_periodically_flushes_buffer(
    adapter: MagicMock, repository: AsyncMock,
) -> None:
    writer = _make_writer(
        adapter, repository, flush_interval_s=0.05, max_buffer_size=100,
    )

    async def producer() -> None:
        q: asyncio.Queue[bytes | None] = asyncio.Queue()
        await q.put(_make_frame())
        # consume_hub съест один фрейм и встанет ждать на queue.get()
        await writer.consume_hub(q)

    # consume_hub блокирует — кладём один фрейм, потом ждём фоновый flush,
    # потом отправляем sentinel.
    q: asyncio.Queue[bytes | None] = asyncio.Queue()
    await q.put(_make_frame())

    consume_task = asyncio.create_task(writer.consume_hub(q))
    flusher_task = asyncio.create_task(writer.run_background_flusher())

    # Дать фоновому таймеру отработать минимум один раз.
    await asyncio.sleep(0.15)
    # На этом этапе фрейм-1 должен быть уже записан.
    assert writer.written_total == 1

    # Останавливаем подписчик и фоновый таймер.
    await q.put(None)
    writer.stop()
    await consume_task
    await flusher_task

    assert writer.written_total == 1


async def test_stop_terminates_background_flusher(
    adapter: MagicMock, repository: AsyncMock,
) -> None:
    writer = _make_writer(adapter, repository, flush_interval_s=0.05)
    task = asyncio.create_task(writer.run_background_flusher())

    # Дать фоновому таймеру войти в sleep, затем попросить остановку.
    await asyncio.sleep(0.02)
    writer.stop()
    await asyncio.wait_for(task, timeout=0.5)

    assert task.done()
    # Финальный flush в finally вызван — но буфер пуст, в БД ничего не ушло.
    repository.insert_batch.assert_not_awaited()


async def test_cancelled_consume_hub_still_flushes_in_finally(
    adapter: MagicMock, repository: AsyncMock,
) -> None:
    writer = _make_writer(adapter, repository, max_buffer_size=100)
    q: asyncio.Queue[bytes | None] = asyncio.Queue()
    await q.put(_make_frame())
    await q.put(_make_frame())
    # sentinel НЕ кладём — даём task встать на queue.get(), потом отменяем.

    task = asyncio.create_task(writer.consume_hub(q))
    # Подождать, чтобы оба фрейма успели попасть в буфер.
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert writer.written_total == 2
