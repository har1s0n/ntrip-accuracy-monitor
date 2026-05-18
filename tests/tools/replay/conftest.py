"""Подготовленные данные для тестов воспроизведения.

Тестовые RTCM-кадры синтезируем через crc24q из framer — это даёт
самодостаточные unit-тесты без зависимости от лабораторных файлов.
NMEA-сообщения захардкожены здесь же.

Параллельно есть метод lab_captures_dir, который ищет каталог
captures/lab_*/ — если он есть на локальной машине, дополнительные
интеграционные тесты могут к нему обращаться через pytest.skip(...).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ntrip_accuracy_monitor.protocols.ntrip._framer import crc24q


def _make_rtcm_frame(msg_type: int, payload_tail: bytes = b"\x00" * 8) -> bytes:
    """Собрать синтетический RTCM 3.x кадр с валидной CRC-24Q."""
    type_bytes = bytes([msg_type >> 4, (msg_type & 0x0F) << 4])
    payload = type_bytes + payload_tail
    length = len(payload)
    if length > 1023:
        raise ValueError("полезная нагрузка > 1023 байт, не помещается в 10-битную длину")
    header = bytes([0xD3, (length >> 8) & 0x03, length & 0xFF])
    crc = crc24q(header + payload)
    return header + payload + bytes([(crc >> 16) & 0xFF, (crc >> 8) & 0xFF, crc & 0xFF])


@pytest.fixture
def synth_rtcm_clean(tmp_path: Path) -> Path:
    """Файл из трёх валидных RTCM-кадров, без мусора."""
    data = b"".join([
        _make_rtcm_frame(1004),
        _make_rtcm_frame(1012),
        _make_rtcm_frame(1019, b"\x11" * 64),
    ])
    path = tmp_path / "clean.bin"
    path.write_bytes(data)
    return path


@pytest.fixture
def synth_rtcm_mixed(tmp_path: Path) -> Path:
    """Файл с мусором между кадрами (имитация смешанного RTCM 2.x + 3.x).

    Между валидными кадрами вставлены байты в диапазоне 0x40..0x7F —
    framer должен их отбросить через on_resync.
    """
    garbage_a = bytes(range(0x40, 0x70))  # 48 «чужих» байт
    garbage_b = bytes(range(0x50, 0x80))  # 48 «чужих» байт
    data = (
        garbage_a
        + _make_rtcm_frame(1006)
        + garbage_b
        + _make_rtcm_frame(1007)
        + garbage_a
        + _make_rtcm_frame(1033)
    )
    path = tmp_path / "mixed.bin"
    path.write_bytes(data)
    return path


@pytest.fixture
def synth_rtcm_empty(tmp_path: Path) -> Path:
    """Пустой файл."""
    path = tmp_path / "empty.bin"
    path.write_bytes(b"")
    return path


@pytest.fixture
def synth_rtcm_truncated(tmp_path: Path) -> Path:
    """Валидный кадр + второй обрезан в середине."""
    full = _make_rtcm_frame(1004)
    partial = _make_rtcm_frame(1012)[:5]  # только заголовок, без полезной нагрузки и CRC
    path = tmp_path / "truncated.bin"
    path.write_bytes(full + partial)
    return path


# ---------- NMEA-тестовые наборы ----------

# Три эпохи по 5 сообщений. Все с валидным XOR. Координаты — синтетика.
_NMEA_EPOCH_TEMPLATE = (
    "$GPGGA,120000.00,5548.165200,N,03735.412300,E,4,12,0.8,256.100,M,14.500,M,1.0,0123*XOR\r\n"
    "$GPGST,120000.00,1.0,0.015,0.012,0.0,0.013,0.014,0.025*XOR\r\n"
    "$GPGSA,A,3,01,02,03,04,05,,,,,,,1.5,0.8,1.2*XOR\r\n"
    "$GPRMC,120000.00,A,5548.165200,N,03735.412300,E,0.0,0.0,140526,,*XOR\r\n"
    "$GPZDA,120000.00,14,05,2026,00,00*XOR\r\n"
)


def _compute_xor(body: bytes) -> bytes:
    x = 0
    for b in body:
        x ^= b
    return f"{x:02X}".encode("ascii")


def _fix_checksums(text: str) -> bytes:
    out = bytearray()
    for line in text.splitlines(keepends=True):
        bline = line.encode("ascii")
        if bline.startswith(b"$") and b"*XOR" in bline:
            eol_len = 2 if bline.endswith(b"\r\n") else (1 if bline.endswith(b"\n") else 0)
            tail = bline[-eol_len:] if eol_len else b""
            star_idx = bline.index(b"*")
            body = bline[1:star_idx]
            out += b"$" + body + b"*" + _compute_xor(body) + tail
        else:
            out += bline
    return bytes(out)


@pytest.fixture
def synth_nmea(tmp_path: Path) -> Path:
    """NMEA-файл из трёх эпох RTK-фикса."""
    text = _NMEA_EPOCH_TEMPLATE * 3
    # сдвинем время в каждой следующей эпохе для реалистичности
    lines = text.splitlines(keepends=True)
    epoch_size = 5
    for i in range(3):
        offset = i * epoch_size
        time_str = f"12000{i}.00"
        for j in range(epoch_size):
            lines[offset + j] = lines[offset + j].replace("120000.00", time_str)
    raw = _fix_checksums("".join(lines))
    path = tmp_path / "rover.nmea"
    path.write_bytes(raw)
    return path


@pytest.fixture
def free_tcp_port() -> int:
    """Подобрать свободный TCP-порт на loopback."""
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
