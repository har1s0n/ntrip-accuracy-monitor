# tests/protocols/ntrip/test_chunked.py

from __future__ import annotations

import asyncio

import pytest

from ntrip_accuracy_monitor.protocols.ntrip._chunked import (
    ChunkedDecodeError,
    ChunkedReader,
)


def _reader_with(data: bytes) -> asyncio.StreamReader:
    r = asyncio.StreamReader()
    r.feed_data(data)
    r.feed_eof()
    return r


async def _drain(reader: ChunkedReader) -> bytes:
    out = bytearray()
    while True:
        chunk = await reader.read(4096)
        if not chunk:
            return bytes(out)
        out.extend(chunk)


@pytest.mark.asyncio
async def test_chunked_basic() -> None:
    upstream = _reader_with(b"5\r\nHELLO\r\n3\r\nABC\r\n0\r\n\r\n")
    assert await _drain(ChunkedReader(upstream)) == b"HELLOABC"


@pytest.mark.asyncio
async def test_chunked_with_initial_buffer() -> None:
    # Simulate the handshake leftover: first chunk-size + part of payload
    # were already pulled off the socket while parsing headers.
    upstream = _reader_with(b"LO\r\n3\r\nABC\r\n0\r\n\r\n")
    cr = ChunkedReader(upstream, initial_buffer=b"5\r\nHEL")
    assert await _drain(cr) == b"HELLOABC"


@pytest.mark.asyncio
async def test_chunked_extension_in_header() -> None:
    upstream = _reader_with(b"5;name=value\r\nHELLO\r\n0\r\n\r\n")
    assert await _drain(ChunkedReader(upstream)) == b"HELLO"


@pytest.mark.asyncio
async def test_chunked_uppercase_hex_size() -> None:
    # 0x1A = 26 bytes
    payload = b"A" * 26
    upstream = _reader_with(b"1A\r\n" + payload + b"\r\n0\r\n\r\n")
    assert await _drain(ChunkedReader(upstream)) == payload


@pytest.mark.asyncio
async def test_chunked_partial_reads_via_small_n() -> None:
    upstream = _reader_with(b"a\r\n0123456789\r\n0\r\n\r\n")
    cr = ChunkedReader(upstream)
    parts: list[bytes] = []
    while True:
        chunk = await cr.read(3)
        if not chunk:
            break
        parts.append(chunk)
    assert b"".join(parts) == b"0123456789"


@pytest.mark.asyncio
async def test_chunked_invalid_size_raises() -> None:
    upstream = _reader_with(b"GARBAGE\r\nHELLO\r\n0\r\n\r\n")
    with pytest.raises(ChunkedDecodeError):
        await _drain(ChunkedReader(upstream))


@pytest.mark.asyncio
async def test_chunked_eof_mid_chunk_raises() -> None:
    # Truncated mid-payload: upstream EOF arrives before the chunk is full.
    upstream = _reader_with(b"5\r\nHEL")  # promised 5 bytes, sent 3
    with pytest.raises(ChunkedDecodeError):
        await _drain(ChunkedReader(upstream))
