"""Серверный парсер NTRIP-запросов от клиентов.

Зеркало protocols/ntrip/_handshake.py (клиентский парсер ответа кастера).

Поддерживаемые запросы:
  GET / HTTP/1.1\r\n         — sourcetable
  GET /<mountpoint> HTTP/1.0  — V1 ICY-стиль (без Ntrip-Version)
  GET /<mountpoint> HTTP/1.1\r\nNtrip-Version: Ntrip/2.0\r\n... — V2

Аутентификация: только Basic.
"""

from __future__ import annotations

import asyncio
import base64
from dataclasses import dataclass
from typing import Final

_MAX_HEADER_BYTES: Final = 8192


class HandshakeError(Exception):
    """Невалидный или превышающий лимит запрос клиента."""


@dataclass(frozen=True, slots=True)
class NtripRequest:
    method: str
    target: str
    http_version: str
    ntrip_version: int
    headers: dict[str, str]

    @property
    def mountpoint(self) -> str:
        return self.target.lstrip("/")

    @property
    def is_sourcetable_request(self) -> bool:
        return self.target == "/" or self.mountpoint == ""

    def basic_auth(self) -> tuple[str, str] | None:
        h = self.headers.get("authorization", "")
        if not h.lower().startswith("basic "):
            return None
        try:
            decoded = base64.b64decode(h[6:].strip()).decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            return None
        if ":" not in decoded:
            return None
        user, _, password = decoded.partition(":")
        return (user, password)


def parse_request(raw: bytes) -> NtripRequest:
    """Распарсить заголовочную часть NTRIP-запроса (до \\r\\n\\r\\n)."""
    if len(raw) > _MAX_HEADER_BYTES:
        raise HandshakeError(f"header too long: {len(raw)} bytes")

    text = raw.decode("ascii", errors="replace")
    lines = text.split("\r\n")
    if not lines or not lines[0]:
        raise HandshakeError("empty request")

    parts = lines[0].split(" ")
    if len(parts) != 3:
        raise HandshakeError(f"malformed request line: {lines[0]!r}")
    method, target, http_version = parts
    if method != "GET":
        raise HandshakeError(f"unsupported method: {method!r}")
    if http_version not in {"HTTP/1.0", "HTTP/1.1"}:
        raise HandshakeError(f"unsupported HTTP version: {http_version!r}")

    headers: dict[str, str] = {}
    for line in lines[1:]:
        if line == "":
            break
        if ":" not in line:
            continue
        name, _, value = line.partition(":")
        headers[name.strip().lower()] = value.strip()

    ntrip_ver_header = headers.get("ntrip-version", "").lower()
    ntrip_version = 2 if "ntrip/2.0" in ntrip_ver_header else 1

    return NtripRequest(
        method=method,
        target=target,
        http_version=http_version,
        ntrip_version=ntrip_version,
        headers=headers,
    )


async def read_request(
    reader: asyncio.StreamReader,
    *,
    timeout_s: float = 10.0,
) -> NtripRequest:
    """Читать заголовок до \\r\\n\\r\\n с таймаутом и лимитом."""
    buf = b""
    async with asyncio.timeout(timeout_s):
        while b"\r\n\r\n" not in buf:
            chunk = await reader.read(1024)
            if not chunk:
                if not buf:
                    raise HandshakeError("client disconnected before sending request")
                break
            buf += chunk
            if len(buf) > _MAX_HEADER_BYTES:
                raise HandshakeError("header exceeds limit")
    return parse_request(buf)
