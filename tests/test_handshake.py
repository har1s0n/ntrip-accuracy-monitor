"""Handshake parser tests using real wire captures."""

from __future__ import annotations

import pytest

from ntrip_accuracy_monitor.protocols.ntrip._handshake import (
    HandshakeParseError,
    build_request,
    parse_response,
)

# Real EFT RS3 caster response captured via netcat.
# Status-line + 3 NTRIP-1.0 headers, single CRLFs, RTCM3 body starts at 0xD3.
RS3_RAW: bytes = (
    b"ICY 200 OK\r\n"
    b"Ntrip-Version: Ntrip/1.0\r\n"
    b"Server: NTRIP Caster 1.0\r\n"
    b"Date: Thu, 30 Apr 2026 13:08:10 UTC\r\n"
    b"\xd3\x00\x15\x3e\xe0\x07\x03\x86\xa1\xa9\x86\x59\x85\x09\x3c\x07"
    b"\x4f\x0c\x41\x60\xfc\x53\x00\x00\xab\x4a\x08"
)

# Simulated BKG-style HTTP/1.1 response: double CRLF separates headers and body.
BKG_RAW: bytes = (
    b"HTTP/1.1 200 OK\r\n"
    b"Ntrip-Version: Ntrip/2.0\r\n"
    b"Server: NTRIP BKG Caster/2.0\r\n"
    b"Content-Type: gnss/data\r\n"
    b"Connection: close\r\n"
    b"Transfer-Encoding: chunked\r\n"
    b"\r\n"
    b"\xd3\x00\x13"  # start of an RTCM frame header
)


def test_parse_rs3_icy_format() -> None:
    resp = parse_response(RS3_RAW)
    assert resp is not None
    assert resp.protocol == "ICY"
    assert resp.status_code == 200
    assert resp.status_reason == "OK"
    assert resp.headers["ntrip-version"] == "Ntrip/1.0"
    assert resp.headers["server"] == "NTRIP Caster 1.0"
    assert resp.leftover.startswith(b"\xd3\x00\x15")
    assert len(resp.leftover) == 27


def test_parse_bkg_http_format() -> None:
    resp = parse_response(BKG_RAW)
    assert resp is not None
    assert resp.protocol == "HTTP/1.1"
    assert resp.status_code == 200
    assert resp.headers["ntrip-version"] == "Ntrip/2.0"
    assert resp.headers["transfer-encoding"] == "chunked"
    assert resp.leftover == b"\xd3\x00\x13"


def test_parse_partial_returns_none() -> None:
    # Truncate mid-header: parser asks for more.
    assert parse_response(RS3_RAW[:25]) is None


def test_parse_only_status_line_returns_none() -> None:
    # Status-line is complete but no body byte yet — must keep buffering.
    assert parse_response(b"ICY 200 OK\r\nNtrip-Version: Ntrip/1.0\r\n") is None


def test_parse_401_unauthorized() -> None:
    raw = (
        b"HTTP/1.1 401 Unauthorized\r\n"
        b"Server: NTRIP BKG Caster\r\n"
        b"WWW-Authenticate: Basic realm=\"/\"\r\n"
        b"\r\n"
    )
    resp = parse_response(raw)
    assert resp is not None
    assert resp.status_code == 401
    assert resp.status_reason == "Unauthorized"
    assert resp.leftover == b""


def test_parse_sourcetable() -> None:
    raw = (
        b"SOURCETABLE 200 OK\r\n"
        b"Server: NTRIP BKG Caster\r\n"
        b"Content-Type: gnss/sourcetable\r\n"
        b"\r\n"
        b"STR;MOUNT1;...\r\n"
    )
    resp = parse_response(raw)
    assert resp is not None
    assert resp.protocol == "SOURCETABLE"
    assert resp.status_code == 200
    assert resp.leftover.startswith(b"STR;MOUNT1")


def test_parse_malformed_status_line_raises() -> None:
    with pytest.raises(HandshakeParseError):
        parse_response(b"NOTAREAL_RESPONSE\r\n\r\n")


def test_build_request_v1() -> None:
    req = build_request(
        host="10.20.20.107",
        port=6610,
        mountpoint="TESTRS3CAST0",
        username="test_caster_rs3",
        password="password",
        ntrip_version="1.0",
        user_agent="NTRIP probe/1.0",
    )
    text = req.decode("ascii")
    assert text.startswith("GET /TESTRS3CAST0 HTTP/1.0\r\n")
    assert "Host: 10.20.20.107:6610\r\n" in text
    assert "Authorization: Basic dGVzdF9jYXN0ZXJfcnMzOnBhc3N3b3JkCg==" not in text  # no trailing newline
    assert "Authorization: Basic dGVzdF9jYXN0ZXJfcnMzOnBhc3N3b3Jk\r\n" in text
    assert "Ntrip-Version" not in text  # only sent for 2.0
    assert text.endswith("\r\n\r\n")


def test_build_request_v2() -> None:
    req = build_request(
        host="euref-ip.net",
        port=2101,
        mountpoint="ISRN00ITA0",
        username=None,
        password=None,
        ntrip_version="2.0",
        user_agent="NTRIP probe/1.0",
    )
    text = req.decode("ascii")
    assert text.startswith("GET /ISRN00ITA0 HTTP/1.1\r\n")
    assert "Ntrip-Version: Ntrip/2.0\r\n" in text
    assert "Authorization" not in text  # no creds passed


def test_icy_body_rtcm2_printable_is_not_header() -> None:
    # Ответ RS3 в RTD: ICY + заголовки + RTCM 2.x (печатный, без CRLF, без ':').
    buf = (
        b"ICY 200 OK\r\n"
        b"Ntrip-Version: Ntrip/1.0\r\n"
        b"Server: NTRIP Caster 1.0\r\n"
        b"Date: Tue, 23 Jun 2026 12:09:14 UTC\r\n"
        b"fACxNxy@YpwCz[guB}LvQ"  # RTCM 2.x: 0x40-0x7F, есть '@','[','}'
        b"\xd3\x00\x15>\xe0\x07\x03\x86"  # RTCM 3.x кадр
    )
    resp = parse_response(buf)
    assert resp is not None
    assert resp.protocol == "ICY"
    assert resp.status_code == 200
    assert resp.headers["ntrip-version"] == "Ntrip/1.0"
    assert resp.headers["server"] == "NTRIP Caster 1.0"
    assert resp.leftover.startswith(b"fACxNxy@")  # тело с первого байта RTCM 2.x


def test_icy_body_rtcm3_binary_still_parses() -> None:
    # Регрессия: тело RTCM 3.x (0xD3, непечатный) — путь, что работал в RTK.
    buf = b"ICY 200 OK\r\nServer: NTRIP Caster 1.0\r\n\xd3\x00\x13>\xe0\x07"
    resp = parse_response(buf)
    assert resp is not None and resp.leftover.startswith(b"\xd3")


def test_icy_incomplete_header_waits() -> None:
    # Недописанный заголовок не должен опознаваться как тело.
    assert parse_response(b"ICY 200 OK\r\nNtrip-Versio") is None
    assert parse_response(b"ICY 200 OK\r\nNtrip-Version: Ntri") is None
