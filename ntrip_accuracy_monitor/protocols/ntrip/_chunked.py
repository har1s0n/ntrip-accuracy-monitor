"""HTTP/1.1 chunked transfer-encoding decoder for NTRIP 2.0 streams.

NTRIP 2.0 (RTCM 10410.1 Amend 1 §3.1.7) requires the caster to send
the RTCM body framed as ``<hex_size>\\r\\n<bytes>\\r\\n...0\\r\\n\\r\\n``.
This module wraps an upstream byte source (typically asyncio.StreamReader)
and exposes a ``read(n)`` interface that yields decoded body bytes,
hiding the chunked envelope from the framer.

Reference: RFC 7230 §4.1 (Transfer-Encoding: chunked).
"""

from __future__ import annotations

import asyncio
from typing import Final

_FILL_CHUNK: Final[int] = 4096


class ChunkedDecodeError(Exception):
    """Malformed chunked stream — protocol violation by the upstream."""


class ChunkedReader:
    """Decodes chunked transfer-encoding from an asyncio.StreamReader.

    Implements the same ``async read(n) -> bytes`` shape as StreamReader
    so it can be used interchangeably as the input to the RTCM framer.
    """

    def __init__(
        self,
        upstream: asyncio.StreamReader,
        *,
        initial_buffer: bytes = b"",
    ) -> None:
        self._upstream = upstream
        self._buf = bytearray(initial_buffer)
        self._chunk_left: int = 0
        self._eof: bool = False

    async def read(self, n: int = -1) -> bytes:
        """Return up to ``n`` decoded body bytes, ``b''`` at end-of-stream."""
        if self._eof:
            return b""
        cap = _FILL_CHUNK if n < 0 else n
        out = bytearray()
        while len(out) < cap:
            if self._chunk_left == 0:
                if not await self._read_chunk_header():
                    self._eof = True
                    break
            take = min(cap - len(out), self._chunk_left)
            data = await self._take(take)
            if not data:
                self._eof = True
                break
            out.extend(data)
            self._chunk_left -= len(data)
            if self._chunk_left == 0:
                await self._consume_crlf()
            # Return as soon as we have *some* data — preserves streaming
            # semantics expected by the framer.
            if out:
                break
        return bytes(out)

    async def _read_chunk_header(self) -> bool:
        line = await self._read_line()
        size_str = line.split(b";", 1)[0].strip()
        if not size_str:
            raise ChunkedDecodeError(f"empty chunk-size line: {line!r}")
        try:
            size = int(size_str, 16)
        except ValueError as exc:
            raise ChunkedDecodeError(
                f"non-hex chunk-size: {size_str!r}"
            ) from exc
        if size == 0:
            # Drain optional trailers + final CRLF.
            while True:
                trailer = await self._read_line()
                if not trailer:
                    return False
        self._chunk_left = size
        return True

    async def _read_line(self) -> bytes:
        while True:
            idx = self._buf.find(b"\r\n")
            if idx != -1:
                line = bytes(self._buf[:idx])
                del self._buf[: idx + 2]
                return line
            chunk = await self._upstream.read(_FILL_CHUNK)
            if not chunk:
                raise ChunkedDecodeError("upstream EOF inside chunked frame")
            self._buf.extend(chunk)

    async def _take(self, n: int) -> bytes:
        while len(self._buf) < n:
            chunk = await self._upstream.read(_FILL_CHUNK)
            if not chunk:
                # Mid-chunk EOF is a chunked-protocol violation: the
                # caster promised `chunk_left` more bytes and then
                # closed the connection. Surface as a typed error so
                # the supervisor reconnects with a clear cause rather
                # than treating partial data as a successful read.
                raise ChunkedDecodeError(
                    f"upstream EOF inside chunk: need {n} bytes, "
                    f"have {len(self._buf)}"
                )
            self._buf.extend(chunk)
        data = bytes(self._buf[:n])
        del self._buf[:n]
        return data

    async def _consume_crlf(self) -> None:
        while len(self._buf) < 2:
            chunk = await self._upstream.read(_FILL_CHUNK)
            if not chunk:
                raise ChunkedDecodeError("upstream EOF expecting CRLF")
            self._buf.extend(chunk)
        if self._buf[:2] != b"\r\n":
            raise ChunkedDecodeError(
                f"expected CRLF after chunk, got {bytes(self._buf[:2])!r}"
            )
        del self._buf[:2]
