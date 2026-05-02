"""RTCM3 framer tests against real bytes from EFT RS3 caster."""

from __future__ import annotations

import asyncio

import pytest

from ntrip_accuracy_monitor.protocols.ntrip._framer import (
    crc24q,
    extract_msg_type,
    stream_rtcm_frames,
)

# Real RTCM3 payload captured from RS3 — bytes after the ICY headers.
# Frames here: 1006 (DF002=1006, station ARP), 1033, 1077, 1087, ...
RS3_RTCM_DUMP: bytes = bytes.fromhex(
    "d300153ee0070386a1a9865985093c074f0c4160fc530000ab4a08"
    "d300133ef0070e4144564e554c4c414e54454e4e4100ba9158"
)


# That's only the first two frames from the nc dump; trimmed for unit-test
# brevity. CRC bytes are intact — the framer must validate them.


def test_crc24q_known_vector() -> None:
    assert crc24q(b"") == 0
    # Frame 1 from RS3_RTCM_DUMP: header + payload(21) + CRC(3) = 27 bytes.
    frame = RS3_RTCM_DUMP[:27]
    body, crc_bytes = frame[:-3], frame[-3:]
    expected = (crc_bytes[0] << 16) | (crc_bytes[1] << 8) | crc_bytes[2]
    assert expected == 0xAB4A08
    assert crc24q(body) == expected


def test_extract_msg_type() -> None:
    # First 12 bits of payload byte0..byte1.
    payload = bytes.fromhex("3ee007")  # 0x3EE >> 4 = 0x3EE = 1006
    assert extract_msg_type(payload) == 1006


@pytest.mark.asyncio
async def test_framer_yields_two_frames() -> None:
    reader = asyncio.StreamReader()
    reader.feed_data(RS3_RTCM_DUMP)
    reader.feed_eof()

    frames: list[bytes] = []
    resync_calls: list[bytes] = []

    async for frame in stream_rtcm_frames(
        reader, on_resync=resync_calls.append,
    ):
        frames.append(frame)

    assert len(frames) == 2
    assert frames[0][0] == 0xD3
    assert frames[1][0] == 0xD3
    assert resync_calls == []  # clean stream, no resync


@pytest.mark.asyncio
async def test_framer_resyncs_on_garbage_prefix() -> None:
    reader = asyncio.StreamReader()
    reader.feed_data(b"GARBAGE\x00\xff" + RS3_RTCM_DUMP)
    reader.feed_eof()

    frames: list[bytes] = []
    resync_calls: list[bytes] = []

    async for frame in stream_rtcm_frames(
        reader, on_resync=resync_calls.append,
    ):
        frames.append(frame)

    assert len(frames) == 2
    assert resync_calls and resync_calls[0] == b"GARBAGE\x00\xff"


@pytest.mark.asyncio
async def test_framer_drops_frame_with_bad_crc() -> None:
    # Mutate one byte inside the first frame's payload — CRC must fail,
    # framer must drop the preamble and resync to the second frame.
    corrupted = bytearray(RS3_RTCM_DUMP)
    corrupted[5] ^= 0xFF  # flip a payload byte in frame 1
    reader = asyncio.StreamReader()
    reader.feed_data(bytes(corrupted))
    reader.feed_eof()

    frames: list[bytes] = []
    resync_calls: list[bytes] = []

    async for frame in stream_rtcm_frames(
        reader, on_resync=resync_calls.append,
    ):
        frames.append(frame)

    # We should still get the second frame (intact); the first is dropped
    # via incremental resync (one byte at a time).
    assert len(frames) == 1
    assert frames[0][0] == 0xD3
    assert resync_calls  # at least one resync event recorded


@pytest.mark.asyncio
async def test_framer_initial_buffer_used() -> None:
    # Simulate handshake leftover: half a frame already in initial_buffer.
    split = 10
    reader = asyncio.StreamReader()
    reader.feed_data(RS3_RTCM_DUMP[split:])
    reader.feed_eof()

    frames: list[bytes] = []
    async for frame in stream_rtcm_frames(
        reader, initial_buffer=RS3_RTCM_DUMP[:split],
    ):
        frames.append(frame)

    assert len(frames) == 2
