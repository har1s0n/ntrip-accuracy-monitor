# tests/test_protocols_rtcm_adapter.py

"""Тесты RtcmAdapter на синтетической фикстуре, собранной руками."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from ntrip_accuracy_monitor.protocols.ntrip._framer import crc24q
from ntrip_accuracy_monitor.protocols.rtcm import (
    RtcmAdapter,
    RtcmParseError,
)


def _build_1005_frame(station_id: int = 1234) -> bytes:
    """Собрать минимальный валидный RTCM 1005 (Stationary RTK Reference Station ARP).

    Layout (RTCM 10403.x §5.7) — 152 бита payload:
      DF002 (12) | DF003 (12) | DF021 (6) | DF022 (1) | DF023 (1) | DF024 (1) |
      DF141 (1)  | DF025 (38) | DF142 (1) | reserved (1) | DF026 (38) |
      DF364 (2)  | DF027 (38)
    Все ARP-координаты и индикаторы = 0; станция — GPS+GLO.
    """
    if not 0 <= station_id <= 0xFFF:
        raise ValueError(f"station_id={station_id} не помещается в 12 бит DF003")
    bits = 0
    bit_count = 0

    def push(value: int, n: int) -> None:
        nonlocal bits, bit_count
        if value < 0 or value >= (1 << n):
            raise ValueError(f"value={value} не помещается в {n} бит")
        bits = (bits << n) | (value & ((1 << n) - 1))
        bit_count += n

    push(1005, 12)  # DF002
    push(station_id, 12)  # DF003
    push(0, 6)  # DF021 ITRF realization year
    push(1, 1)  # DF022 GPS indicator
    push(1, 1)  # DF023 GLO indicator
    push(0, 1)  # DF024 Galileo indicator
    push(0, 1)  # DF141 reference station indicator
    push(0, 38)  # DF025 ARP X
    push(0, 1)  # DF142 single-receiver oscillator indicator
    push(0, 1)  # reserved
    push(0, 38)  # DF026 ARP Y
    push(0, 2)  # DF364 quarter cycle indicator
    push(0, 38)  # DF027 ARP Z

    assert bit_count == 152
    payload = bits.to_bytes(19, byteorder="big")

    # Frame: D3 + length(10 bits across 2 bytes) + payload + CRC-24Q
    length = len(payload)
    body = bytes([0xD3, (length >> 8) & 0x03, length & 0xFF]) + payload
    return body + crc24q(body).to_bytes(3, byteorder="big")


class TestRtcmAdapter:
    def test_parses_1005_extracts_station_id(self) -> None:
        adapter = RtcmAdapter()
        before = datetime.now(UTC)
        msg = adapter.parse(_build_1005_frame(station_id=4042))
        after = datetime.now(UTC)

        assert msg.message_type == 1005
        assert msg.station_id == 4042
        assert msg.epoch_time_ms is None  # 1005 — нет эпохи
        assert msg.received_at.tzinfo is UTC
        assert before <= msg.received_at <= after
        assert msg.raw[0] == 0xD3

    def test_completely_corrupt_bytes_raise(self) -> None:
        adapter = RtcmAdapter()
        with pytest.raises(RtcmParseError):
            adapter.parse(b"\x00\x01\x02\x03\x04\x05")  # без D3-преамбулы

    def test_short_input_raises(self) -> None:
        adapter = RtcmAdapter()
        with pytest.raises(RtcmParseError):
            adapter.parse(b"\xd3\x00")  # короче минимума

    def test_unknown_type_falls_back_to_raw_extraction(self) -> None:
        """Если pyrtcm падает на неизвестном типе — message_type извлекается из сырых байт."""
        # Тип = 0x270 (= 624, заведомо неизвестный pyrtcm).
        # raw[3:5] = 0x27, 0x0F → (0x27 << 4) | (0x0F >> 4) = 0x270.
        # CRC заведомо мусорный, но adapter уже не валидирует CRC (доверяет вызывающему).
        raw = bytes([0xD3, 0x00, 0x02, 0x27, 0x0F, 0x00, 0xCC, 0xCC, 0xCC])
        adapter = RtcmAdapter()
        msg = adapter.parse(raw)
        assert msg.message_type == 0x270
        assert msg.station_id is None
