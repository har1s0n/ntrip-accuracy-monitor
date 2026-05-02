from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager, suppress

import pytest

from ntrip_accuracy_monitor.protocols.backoff import BackoffPolicy
from ntrip_accuracy_monitor.protocols.nmea.messages import GgaRecord, NmeaRecord
from ntrip_accuracy_monitor.protocols.nmea.transport import NmeaTcpClient

# ---- Helpers ---------------------------------------------------------------
ScriptFn = Callable[[asyncio.StreamReader, asyncio.StreamWriter], Awaitable[None]]


def _nmea_with_checksum(body: str) -> bytes:
    """Собрать NMEA-фрейм `$<body>*XX\\r\\n` с честным XOR'ом."""
    cs = 0
    for ch in body.encode("ascii"):
        cs ^= ch
    return f"${body}*{cs:02X}\r\n".encode("ascii")


# Реальные NMEA-полезные нагрузки (без $ и *XX) — checksum считается helper'ом.
_GGA_BODY_RTK_FIXED = (
    "GPGGA,123519,4807.038,N,01131.000,E,4,08,0.9,545.4,M,46.9,M,1.2,0123"
)
_GGA_BODY_RTK_FLOAT = (
    "GPGGA,123520,4807.039,N,01131.001,E,5,08,0.9,545.5,M,46.9,M,1.2,0123"
)
_GGA_BODY_DGPS = (
    "GPGGA,123521,4807.040,N,01131.002,E,2,08,0.9,545.6,M,46.9,M,2.5,0123"
)


class _LoopbackServer:
    """Управляет последовательностью TCP-коннектов: один script = один коннект."""

    def __init__(self) -> None:
        self.scripts: list[ScriptFn] = []
        self.connections_received = 0

    def script(self, fn: ScriptFn) -> None:
        self.scripts.append(fn)

    async def _handler(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter,
    ) -> None:
        idx = self.connections_received
        self.connections_received += 1
        try:
            if idx < len(self.scripts):
                with suppress(ConnectionResetError, BrokenPipeError):
                    await self.scripts[idx](reader, writer)
        finally:
            with suppress(OSError):
                writer.close()
                await writer.wait_closed()


@asynccontextmanager
async def _loopback() -> AsyncIterator[tuple[int, _LoopbackServer]]:
    lb = _LoopbackServer()
    server = await asyncio.start_server(lb._handler, "127.0.0.1", 0)
    assert server.sockets is not None
    port = server.sockets[0].getsockname()[1]
    async with server:
        yield port, lb


def _fast_backoff() -> BackoffPolicy:
    return BackoffPolicy(
        initial_delay_s=0.05, max_delay_s=0.2, multiplier=2.0, jitter=0.0,
    )


def _make_client(port: int, *, stall_timeout_s: float = 1.0) -> NmeaTcpClient:
    return NmeaTcpClient(
        stream_id="test",
        host="127.0.0.1",
        port=port,
        connect_timeout_s=1.0,
        stall_timeout_s=stall_timeout_s,
        backoff=_fast_backoff(),
    )


async def _take(client: NmeaTcpClient, n: int, *, timeout_s: float = 5.0) -> list[NmeaRecord]:
    """Прочитать ровно n record-ов из клиента или упасть по таймауту."""
    out: list[NmeaRecord] = []

    async def consume() -> None:
        async for rec in client:
            out.append(rec)
            if len(out) >= n:
                return

    await asyncio.wait_for(consume(), timeout=timeout_s)
    return out


# ---- Тесты -----------------------------------------------------------------
@pytest.mark.asyncio
async def test_happy_path_three_gga_in_one_burst() -> None:
    async with _loopback() as (port, lb):
        async def script(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            for body in (_GGA_BODY_RTK_FIXED, _GGA_BODY_RTK_FLOAT, _GGA_BODY_DGPS):
                writer.write(_nmea_with_checksum(body))
            await writer.drain()
            await reader.read()  # держим коннект до закрытия клиентом

        lb.script(script)

        async with _make_client(port) as client:
            records = await _take(client, 3)

        assert len(records) == 3
        assert all(isinstance(r, GgaRecord) for r in records)
        assert client.parse_errors == 0
        assert client.checksum_failures == 0


@pytest.mark.asyncio
async def test_line_split_across_two_sends_is_reassembled() -> None:
    async with _loopback() as (port, lb):
        line = _nmea_with_checksum(_GGA_BODY_RTK_FIXED)
        cut = len(line) // 2

        async def script(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            writer.write(line[:cut])
            await writer.drain()
            await asyncio.sleep(0.05)
            writer.write(line[cut:])
            await writer.drain()
            await reader.read()

        lb.script(script)

        async with _make_client(port) as client:
            records = await _take(client, 1)

        assert len(records) == 1
        assert isinstance(records[0], GgaRecord)


@pytest.mark.asyncio
async def test_bad_checksum_in_middle_is_skipped() -> None:
    async with _loopback() as (port, lb):
        good_1 = _nmea_with_checksum(_GGA_BODY_RTK_FIXED)
        good_2 = _nmea_with_checksum(_GGA_BODY_DGPS)
        # Берем корректный фрейм и портим checksum-байт
        bad = _nmea_with_checksum(_GGA_BODY_RTK_FLOAT)
        bad = bad.replace(b"*", b"*")  # no-op, иллюстрация
        # Меняем последний hex-байт чек-суммы на заведомо неверный
        bad = bad[:-4] + b"00" + bad[-2:]

        async def script(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            writer.write(good_1 + bad + good_2)
            await writer.drain()
            await reader.read()

        lb.script(script)

        async with _make_client(port) as client:
            records = await _take(client, 2)

        assert len(records) == 2
        assert client.checksum_failures == 1
        assert client.parse_errors == 0


@pytest.mark.asyncio
async def test_non_nmea_lines_are_dropped_and_counted() -> None:
    async with _loopback() as (port, lb):
        async def script(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            writer.write(b"some firmware log line\r\n")
            writer.write(b"!AIVDM,1,1,,A,xxxx,0*7E\r\n")  # AIS, не GNSS-NMEA
            writer.write(_nmea_with_checksum(_GGA_BODY_RTK_FIXED))
            await writer.drain()
            await reader.read()

        lb.script(script)

        async with _make_client(port) as client:
            records = await _take(client, 1)

        assert len(records) == 1
        assert client.non_nmea_lines == 2
        assert client.checksum_failures == 0
        assert client.parse_errors == 0


@pytest.mark.asyncio
async def test_reconnect_after_server_close() -> None:
    async with _loopback() as (port, lb):
        async def script_1(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            writer.write(_nmea_with_checksum(_GGA_BODY_RTK_FIXED))
            await writer.drain()
            # возврат → handler закрывает writer → клиент видит EOF

        async def script_2(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            writer.write(_nmea_with_checksum(_GGA_BODY_DGPS))
            await writer.drain()
            await reader.read()

        lb.script(script_1)
        lb.script(script_2)

        async with _make_client(port) as client:
            records = await _take(client, 2)

        assert len(records) == 2
        assert client.reconnects >= 1
        assert lb.connections_received == 2


@pytest.mark.asyncio
async def test_stall_timeout_triggers_reconnect() -> None:
    async with _loopback() as (port, lb):
        async def script_1(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            # Молчим — ничего не шлём. reader.read() вернёт b"" когда клиент закроется.
            await reader.read()

        async def script_2(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            writer.write(_nmea_with_checksum(_GGA_BODY_RTK_FIXED))
            await writer.drain()
            await reader.read()

        lb.script(script_1)
        lb.script(script_2)

        client = _make_client(port, stall_timeout_s=0.2)
        async with client:
            records = await _take(client, 1, timeout_s=5.0)

        assert len(records) == 1
        assert client.reconnects >= 1
        assert lb.connections_received >= 2


@pytest.mark.asyncio
async def test_cancel_terminates_promptly() -> None:
    async with _loopback() as (port, lb):
        async def script(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            await reader.read()  # просто держим коннект

        lb.script(script)

        client = _make_client(port, stall_timeout_s=10.0)

        async def consume() -> None:
            async with client:
                async for _ in client:
                    pass

        task = asyncio.create_task(consume())
        await asyncio.sleep(0.2)  # дать подключиться
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=2.0)


@pytest.mark.asyncio
async def test_persistent_reconnect_when_server_initially_unavailable() -> None:
    # Поднимаем сервер, узнаём порт, гасим — клиент должен долбиться, пока сервер не вернётся.
    server_holder = await asyncio.start_server(
        lambda r, w: asyncio.sleep(0), "127.0.0.1", 0,
    )
    assert server_holder.sockets is not None
    port = server_holder.sockets[0].getsockname()[1]
    server_holder.close()
    await server_holder.wait_closed()

    client = _make_client(port)

    async def consume() -> None:
        async with client:
            async for _ in client:
                pass

    task = asyncio.create_task(consume())
    # Пусть пару раз попробует и получит ConnectionRefused
    await asyncio.sleep(0.4)
    assert not task.done(), "клиент не должен падать на недоступном сервере"
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=2.0)
    assert client.reconnects == 0  # коннект не вставал — это не reconnect, это первая попытка
