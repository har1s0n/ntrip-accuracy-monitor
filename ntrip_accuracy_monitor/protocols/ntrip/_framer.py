"""Async RTCM3 frame extractor with CRC-24Q validation.

Reads from an ``asyncio.StreamReader`` and yields one complete RTCM3
frame per iteration. Resyncs byte-by-byte on CRC failure or stray data
between frames — common in real-world casters that occasionally
prepend keep-alive whitespace or NMEA echoes.

Reference: RTCM 10403.x §4 (frame structure), CRC-24Q polynomial
0x1864CFB (Qualcomm CRC, no reflection, init=0).
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from typing import Final, Protocol

_PREAMBLE: Final[int] = 0xD3
_HEADER_LEN: Final[int] = 3
_CRC_LEN: Final[int] = 3
_MAX_PAYLOAD: Final[int] = 1023  # 10-bit length field
_MAX_FRAME: Final[int] = _HEADER_LEN + _MAX_PAYLOAD + _CRC_LEN  # 1029
_READ_CHUNK: Final[int] = 4096


class AsyncByteReader(Protocol):
    async def read(self, n: int = -1) -> bytes: ...


def _build_crc24q_table() -> tuple[int, ...]:
    poly = 0x1864CFB
    table = [0] * 256
    for byte in range(256):
        crc = byte << 16
        for _ in range(8):
            crc <<= 1
            if crc & 0x1000000:
                crc ^= poly
        table[byte] = crc & 0xFFFFFF
    return tuple(table)


_CRC24Q_TABLE: Final[tuple[int, ...]] = _build_crc24q_table()


def crc24q(data: bytes | bytearray | memoryview) -> int:
    """CRC-24Q over ``data`` (init=0)."""
    crc = 0
    for b in data:
        crc = ((crc << 8) & 0xFFFFFF) ^ _CRC24Q_TABLE[(crc >> 16) ^ b]
    return crc


def extract_msg_type(payload: bytes | bytearray | memoryview) -> int:
    """Read DF002 (12-bit message type) from RTCM3 payload."""
    if len(payload) < 2:
        return 0
    return ((payload[0] << 4) | (payload[1] >> 4)) & 0x0FFF


async def stream_rtcm_frames(
    reader: AsyncByteReader,
    *,
    initial_buffer: bytes = b"",
    on_resync: Callable[[bytes], None] | None = None,
) -> AsyncIterator[bytes]:
    """Yield validated RTCM3 frames as raw bytes (preamble..CRC inclusive).

    Args:
        reader: Open StreamReader from ``asyncio.open_connection``.
        initial_buffer: Bytes already pulled off the socket during
            handshake (NtripResponse.leftover). Prepended to the
            internal buffer before the first network read.
        on_resync: Optional callback invoked with the discarded bytes
            whenever the framer drops data during resync (preamble
            search miss or CRC failure). Used to count CRC errors.

    Stops when the underlying reader signals EOF (``read()`` returns
    ``b""``). Does not handle reconnection — that's the caller's job.
    """
    buf = bytearray(initial_buffer)

    while True:
        # Find a preamble.
        idx = buf.find(_PREAMBLE)
        if idx == -1:
            if buf and on_resync is not None:
                on_resync(bytes(buf))
            buf.clear()
            chunk = await reader.read(_READ_CHUNK)
            if not chunk:
                return
            buf.extend(chunk)
            continue
        if idx > 0:
            if on_resync is not None:
                on_resync(bytes(buf[:idx]))
            del buf[:idx]

        # Need at least the 3-byte header to know payload length.
        while len(buf) < _HEADER_LEN:
            chunk = await reader.read(_READ_CHUNK)
            if not chunk:
                return
            buf.extend(chunk)

        # Sanity: high 6 bits of byte 1 are reserved and SHOULD be zero.
        # Some casters violate this; we don't enforce, but if length
        # field decodes >1023 it's structurally impossible.
        payload_len = ((buf[1] & 0x03) << 8) | buf[2]
        if payload_len > _MAX_PAYLOAD:
            # False preamble; drop one byte and retry.
            if on_resync is not None:
                on_resync(bytes(buf[:1]))
            del buf[:1]
            continue

        frame_len = _HEADER_LEN + payload_len + _CRC_LEN
        while len(buf) < frame_len:
            chunk = await reader.read(_READ_CHUNK)
            if not chunk:
                return
            buf.extend(chunk)

        frame = bytes(buf[:frame_len])
        # CRC-24Q is computed over header + payload; field is last 3 bytes BE.
        expected = (frame[-3] << 16) | (frame[-2] << 8) | frame[-1]
        actual = crc24q(memoryview(frame)[: _HEADER_LEN + payload_len])
        if actual != expected:
            # Bad frame — drop preamble byte, keep searching.
            if on_resync is not None:
                on_resync(bytes(buf[:1]))
            del buf[:1]
            continue

        del buf[:frame_len]
        yield frame
