"""NTRIP HTTP-like handshake: request build, response parse.

Pure synchronous helpers, no I/O. Two response formats supported:

  * Standard HTTP/1.x: status-line + headers terminated by \\r\\n\\r\\n,
    body follows. Used by NTRIP 2.0 casters (BKG, IGS, etc).

  * Shoutcast/ICY: 'ICY 200 OK\\r\\n', headers separated by single
    \\r\\n, body begins at the first non-header line (typically the
    RTCM3 preamble 0xD3). Used by NTRIP 1.0 casters (EFT RS3 caster
    and many Chinese OEM implementations).

References: RTCM 10410.1 §3.1.6/3.1.7 (NTRIP v1/v2 handshake).
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
from typing import Final, Literal

_HEADER_KEY_ALLOWED: Final[frozenset[int]] = frozenset(
    # Token chars per RFC 7230 §3.2.6, excluding ':' to allow detection.
    b"!#$%&'*+-.^_`|~0123456789"
    b"ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    b"abcdefghijklmnopqrstuvwxyz"
)

_MAX_HEADER_KEY_LEN: Final[int] = 64


def _all_header_token(bs: bytes) -> bool:
    return all(b in _HEADER_KEY_ALLOWED for b in bs)


@dataclass(frozen=True, slots=True)
class NtripResponse:
    """Parsed handshake response.

    Attributes:
        protocol: Status-line protocol token, e.g. ``"HTTP/1.1"``,
            ``"ICY"``, ``"SOURCETABLE"``. Case-preserved.
        status_code: HTTP status code (200 / 401 / 404 / etc).
        status_reason: Status-line reason phrase, may be empty.
        headers: Lower-cased header keys → trimmed values. Duplicate
            keys keep the last occurrence.
        leftover: Bytes belonging to the response body that were
            already in the buffer when headers ended. Hand off to
            the framer as ``initial_buffer``.
    """

    protocol: str
    status_code: int
    status_reason: str
    headers: dict[str, str]
    leftover: bytes


class HandshakeParseError(Exception):
    """Malformed status-line or header — caster speaks neither HTTP nor ICY."""


def build_request(
    *,
    host: str,
    port: int,
    mountpoint: str,
    username: str | None,
    password: str | None,
    ntrip_version: Literal["1.0", "2.0"],
    user_agent: str,
) -> bytes:
    """Construct the GET request bytes."""
    http_ver = "1.0" if ntrip_version == "1.0" else "1.1"
    lines: list[str] = [
        f"GET /{mountpoint} HTTP/{http_ver}",
        f"Host: {host}:{port}",
        f"User-Agent: {user_agent}",
        "Accept: */*",
        "Connection: close",
    ]
    if ntrip_version == "2.0":
        lines.insert(2, "Ntrip-Version: Ntrip/2.0")
    if username is not None or password is not None:
        token = base64.b64encode(
            f"{username or ''}:{password or ''}".encode("utf-8")
        ).decode("ascii")
        lines.append(f"Authorization: Basic {token}")
    return ("\r\n".join(lines) + "\r\n\r\n").encode("ascii")


def parse_response(buffer: bytes) -> NtripResponse | None:
    """Try to parse a handshake response from ``buffer``.

    Returns:
        ``None`` if the buffer is incomplete (caller should read more).
        Parsed :class:`NtripResponse` once headers are fully present.

    Raises:
        HandshakeParseError: status-line is malformed (wrong shape, no
            digits where a code is expected, etc).
    """
    # Need at least the status-line.
    eol = buffer.find(b"\r\n")
    if eol == -1:
        return None

    status_line = buffer[:eol]
    try:
        protocol, code, reason = _parse_status_line(status_line)
    except ValueError as exc:
        raise HandshakeParseError(
            f"malformed status line: {status_line!r}"
        ) from exc

    rest = buffer[eol + 2:]

    # Standard HTTP: headers terminated by \r\n\r\n (i.e. an empty line).
    # Try this path first — it's unambiguous when present.
    sep = rest.find(b"\r\n\r\n")
    if sep != -1:
        header_blob = rest[:sep]
        body = rest[sep + 4:]
        headers = _parse_header_lines(header_blob.split(b"\r\n"))
        return NtripResponse(
            protocol=protocol,
            status_code=code,
            status_reason=reason,
            headers=headers,
            leftover=body,
        )

    # ICY/shoutcast path: headers separated by single \r\n, body begins
    # at the first line that is NOT a valid 'Key: Value' header. We must
    # have observed at least one such non-header byte to commit, otherwise
    # the buffer is incomplete and we ask for more data.
    return _parse_icy_style(protocol, code, reason, rest)


def _parse_status_line(line: bytes) -> tuple[str, int, str]:
    parts = line.split(b" ", 2)
    if len(parts) < 2:
        raise ValueError(f"need at least protocol + code: {line!r}")
    protocol = parts[0].decode("ascii", errors="replace")
    try:
        code = int(parts[1])
    except ValueError as exc:
        raise ValueError(f"non-numeric status code in {line!r}") from exc
    reason = parts[2].decode("ascii", errors="replace") if len(parts) == 3 else ""
    return protocol, code, reason


def _parse_header_lines(lines: list[bytes]) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in lines:
        if not line:
            continue
        colon = line.find(b":")
        if colon <= 0:
            continue
        key = line[:colon].decode("ascii", errors="replace").strip().lower()
        value = line[colon + 1:].decode("utf-8", errors="replace").strip()
        out[key] = value
    return out


def _parse_icy_style(
    protocol: str, code: int, reason: str, rest: bytes,
) -> NtripResponse | None:
    """Eat header lines until we see a line that is not a header.

    The end-of-headers signal in ICY is the appearance of a line whose
    first byte is not a valid header-name character (e.g. RTCM3 preamble
    0xD3, which is > 0x7F, or a binary byte from the RTCM stream).
    """
    headers: dict[str, str] = {}
    pos = 0
    while True:
        eol = rest.find(b"\r\n", pos)
        if eol == -1:
            # CRLF от pos ещё нет. Различаем незавершённый заголовок и
            # тело без CRLF (RTCM 2.x печатный 0x40–0x7F; RTCM 3.x 0xD3…).
            # Дискриминатор — форма заголовка `token+:`.
            tail = rest[pos:]
            if not tail:
                return None  # ждём данных
            colon = tail.find(b":")
            if colon != -1 and _all_header_token(tail[:colon]):
                return None  # ключ заголовка есть, ждём значение + CRLF
            if (
                colon == -1
                and len(tail) <= _MAX_HEADER_KEY_LEN
                and _all_header_token(tail)
            ):
                return None  # короткий token-прогон — возможно, недописанный ключ
            # Непечатный/непунктуационный байт до ':' (RTCM 2.x '@','[' или
            # RTCM 3.x 0xD3), либо прогон длиннее ключа → здесь начинается тело.
            return NtripResponse(
                protocol=protocol,
                status_code=code,
                status_reason=reason,
                headers=headers,
                leftover=tail,
            )

        line = rest[pos:eol]
        if not line:
            # Empty line — graceful end-of-headers (some hybrids).
            return NtripResponse(
                protocol=protocol,
                status_code=code,
                status_reason=reason,
                headers=headers,
                leftover=rest[eol + 2:],
            )

        first = line[0]
        if first not in _HEADER_KEY_ALLOWED:
            # Non-header line: body starts here. Do NOT consume the CRLF.
            return NtripResponse(
                protocol=protocol,
                status_code=code,
                status_reason=reason,
                headers=headers,
                leftover=rest[pos:],
            )

        colon = line.find(b":")
        if colon <= 0:
            # Looks header-like (printable token start) but no ':' —
            # treat as body, give up trying to parse it.
            return NtripResponse(
                protocol=protocol,
                status_code=code,
                status_reason=reason,
                headers=headers,
                leftover=rest[pos:],
            )

        key = line[:colon].decode("ascii", errors="replace").strip().lower()
        value = line[colon + 1:].decode("utf-8", errors="replace").strip()
        headers[key] = value
        pos = eol + 2
