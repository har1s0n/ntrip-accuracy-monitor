"""Тесты NmeaReplayServer."""

from __future__ import annotations

import asyncio
import math
import re
from pathlib import Path

import pytest

from ntrip_accuracy_monitor.tools.replay import NmeaReplayServer

_GGA_RE = re.compile(rb"^\$G[PNLA]GGA,[^*]+\*[0-9A-F]{2}", re.MULTILINE)


def _validate_nmea_checksum(line: bytes) -> bool:
    """Проверить XOR-контрольную сумму NMEA-предложения."""
    if not line.startswith(b"$") or b"*" not in line:
        return False
    star = line.index(b"*")
    body = line[1:star]
    expected = line[star + 1:star + 3]
    actual = 0
    for b in body:
        actual ^= b
    return f"{actual:02X}".encode("ascii") == expected.upper()


async def _read_until_n_ggas(reader: asyncio.StreamReader, n: int, timeout_s: float) -> bytes:
    """Читать с сокета пока не наберём n GGA или не сработает таймаут."""
    buf = bytearray()
    async with asyncio.timeout(timeout_s):
        while len(_GGA_RE.findall(bytes(buf))) < n:
            chunk = await reader.read(4096)
            if not chunk:
                break
            buf.extend(chunk)
    return bytes(buf)


@pytest.mark.asyncio
async def test_single_client_gets_all_epochs(synth_nmea: Path, free_tcp_port: int) -> None:
    """Один клиент получает все три эпохи и корректно отключается."""
    server = NmeaReplayServer(
        synth_nmea, "127.0.0.1", free_tcp_port, epoch_rate_hz=50.0,
    )
    await server.start()
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", free_tcp_port)
        data = await _read_until_n_ggas(reader, n=3, timeout_s=2.0)
        writer.close()
        await writer.wait_closed()
    finally:
        await server.stop()

    ggas = _GGA_RE.findall(data)
    assert len(ggas) == 3
    for gga in ggas:
        assert _validate_nmea_checksum(gga)


@pytest.mark.asyncio
async def test_passthrough_preserves_original_bytes(
    synth_nmea: Path, free_tcp_port: int,
) -> None:
    """Без force_quality и без jitter байты GGA выходят такими же, как на входе."""
    original = synth_nmea.read_bytes()
    server = NmeaReplayServer(
        synth_nmea, "127.0.0.1", free_tcp_port, epoch_rate_hz=50.0,
    )
    await server.start()
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", free_tcp_port)
        data = await _read_until_n_ggas(reader, n=3, timeout_s=2.0)
        writer.close()
        await writer.wait_closed()
    finally:
        await server.stop()

    # Каждое GGA из вывода должно встречаться в исходном файле как есть
    for gga in _GGA_RE.findall(data):
        assert gga in original


@pytest.mark.asyncio
async def test_force_quality_rewrites_field_and_checksum(
    synth_nmea: Path, free_tcp_port: int,
) -> None:
    """force_quality=2 переписывает поле 6, контрольная сумма пересчитывается."""
    server = NmeaReplayServer(
        synth_nmea, "127.0.0.1", free_tcp_port,
        force_quality=2,
        epoch_rate_hz=50.0,
    )
    await server.start()
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", free_tcp_port)
        data = await _read_until_n_ggas(reader, n=3, timeout_s=2.0)
        writer.close()
        await writer.wait_closed()
    finally:
        await server.stop()

    ggas = _GGA_RE.findall(data)
    assert len(ggas) == 3
    for gga in ggas:
        # Поле 6 — quality
        body = gga[1:gga.index(b"*")]
        fields = body.split(b",")
        assert fields[6] == b"2", f"quality в {gga!r} не переписан"
        assert _validate_nmea_checksum(gga), f"XOR в {gga!r} не пересчитан"


@pytest.mark.asyncio
async def test_position_jitter_statistics(
    synth_nmea: Path, free_tcp_port: int,
) -> None:
    """jitter=2 м даёт сравнимое σ выборки координат при достаточном числе эпох."""
    server = NmeaReplayServer(
        synth_nmea, "127.0.0.1", free_tcp_port,
        position_jitter_m=2.0,
        loop_indefinitely=True,
        epoch_rate_hz=200.0,
    )
    await server.start()
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", free_tcp_port)
        # надо набрать ~200 эпох для устойчивой статистики
        data = await _read_until_n_ggas(reader, n=200, timeout_s=5.0)
        writer.close()
        await writer.wait_closed()
    finally:
        await server.stop()

    ggas = _GGA_RE.findall(data)
    assert len(ggas) >= 200

    # Соберём широты в десятичных градусах
    lats = []
    for gga in ggas[:200]:
        body = gga[1:gga.index(b"*")]
        fields = body.split(b",")
        raw_lat = float(fields[2])
        degrees = int(raw_lat // 100)
        minutes = raw_lat - degrees * 100
        lats.append(degrees + minutes / 60.0)

    mean = sum(lats) / len(lats)
    variance = sum((x - mean) ** 2 for x in lats) / len(lats)
    sigma_deg = math.sqrt(variance)
    sigma_m = sigma_deg * 111_320.0

    # ожидаем σ ≈ 2 м, допуск ±50%
    assert 1.0 <= sigma_m <= 3.0, f"σ выборки = {sigma_m:.2f} м, ожидалось ≈ 2"


@pytest.mark.asyncio
async def test_two_clients_get_same_content(
    synth_nmea: Path, free_tcp_port: int,
) -> None:
    """Два одновременных клиента получают одинаковое содержимое."""
    server = NmeaReplayServer(
        synth_nmea, "127.0.0.1", free_tcp_port, epoch_rate_hz=50.0,
    )
    await server.start()

    async def consume() -> list[bytes]:
        reader, writer = await asyncio.open_connection("127.0.0.1", free_tcp_port)
        try:
            data = await _read_until_n_ggas(reader, n=3, timeout_s=2.0)
        finally:
            writer.close()
            await writer.wait_closed()
        return _GGA_RE.findall(data)

    try:
        ggas_1, ggas_2 = await asyncio.gather(consume(), consume())
    finally:
        await server.stop()

    assert ggas_1 == ggas_2 != []


@pytest.mark.asyncio
async def test_epoch_rate_is_respected(synth_nmea: Path, free_tcp_port: int) -> None:
    """При темпе 10 Гц три эпохи приезжают за ~0.3 с (с разумным допуском)."""
    server = NmeaReplayServer(
        synth_nmea, "127.0.0.1", free_tcp_port, epoch_rate_hz=10.0,
    )
    await server.start()
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", free_tcp_port)
        loop = asyncio.get_running_loop()
        t0 = loop.time()
        await _read_until_n_ggas(reader, n=3, timeout_s=2.0)
        elapsed = loop.time() - t0
        writer.close()
        await writer.wait_closed()
    finally:
        await server.stop()

    # 3 эпохи при 10 Гц — теоретически 0.2 с (между первой и третьей).
    # Берём широкий допуск из-за asyncio-планирования и CI-нагрузки.
    assert 0.15 <= elapsed <= 1.0, f"elapsed={elapsed:.3f}s вне ожидаемого диапазона"


@pytest.mark.asyncio
async def test_loop_indefinitely_restarts_from_beginning(
    synth_nmea: Path, free_tcp_port: int,
) -> None:
    """С loop_indefinitely=True после третьей эпохи приезжает четвёртая (= первая)."""
    server = NmeaReplayServer(
        synth_nmea, "127.0.0.1", free_tcp_port,
        loop_indefinitely=True,
        epoch_rate_hz=100.0,
    )
    await server.start()
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", free_tcp_port)
        data = await _read_until_n_ggas(reader, n=6, timeout_s=2.0)
        writer.close()
        await writer.wait_closed()
    finally:
        await server.stop()

    ggas = _GGA_RE.findall(data)
    assert len(ggas) >= 6
    # первая и четвёртая GGA должны совпасть (новый круг проигрывания)
    assert ggas[0] == ggas[3]


@pytest.mark.asyncio
async def test_stop_disconnects_active_client(
    synth_nmea: Path, free_tcp_port: int,
) -> None:
    """server.stop() корректно завершает работу с активным подключением."""
    server = NmeaReplayServer(
        synth_nmea, "127.0.0.1", free_tcp_port,
        loop_indefinitely=True,
        epoch_rate_hz=50.0,
    )
    await server.start()
    reader, writer = await asyncio.open_connection("127.0.0.1", free_tcp_port)
    # дать клиенту получить хотя бы одну эпоху
    await _read_until_n_ggas(reader, n=1, timeout_s=1.0)

    await server.stop()

    # после stop клиент должен закрыться (read возвращает пустоту)
    async with asyncio.timeout(2.0):
        tail = await reader.read(4096)
    # допустимо: либо EOF, либо остаточные байты, но соединение должно завершиться
    writer.close()
    with pytest.raises((ConnectionResetError, BrokenPipeError, asyncio.IncompleteReadError)):
        try:
            await writer.wait_closed()
        except Exception:
            raise


@pytest.mark.asyncio
async def test_empty_file_raises(tmp_path: Path, free_tcp_port: int) -> None:
    """Файл без GGA — start() поднимает RuntimeError."""
    empty = tmp_path / "empty.nmea"
    empty.write_bytes(b"")
    server = NmeaReplayServer(empty, "127.0.0.1", free_tcp_port)
    with pytest.raises(RuntimeError, match="не найдено ни одной GGA"):
        await server.start()


@pytest.mark.asyncio
async def test_invalid_epoch_rate_raises(synth_nmea: Path, free_tcp_port: int) -> None:
    """epoch_rate_hz <= 0 не принимается."""
    with pytest.raises(ValueError, match="положительным"):
        NmeaReplayServer(synth_nmea, "127.0.0.1", free_tcp_port, epoch_rate_hz=0.0)


@pytest.mark.asyncio
async def test_invalid_jitter_raises(synth_nmea: Path, free_tcp_port: int) -> None:
    """Отрицательный jitter не принимается."""
    with pytest.raises(ValueError, match="отрицательным"):
        NmeaReplayServer(
            synth_nmea, "127.0.0.1", free_tcp_port, position_jitter_m=-1.0,
        )
