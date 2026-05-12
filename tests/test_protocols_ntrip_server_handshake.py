"""Юнит-тесты server-side парсера NTRIP-запросов."""

from __future__ import annotations

import base64

import pytest

from ntrip_accuracy_monitor.protocols.ntrip import HandshakeError, NtripRequest
from ntrip_accuracy_monitor.protocols.ntrip._server_handshake import parse_request


class TestParseRequest:
    def test_v1_get_mountpoint_no_auth(self) -> None:
        raw = b"GET /TESTMOUNT HTTP/1.0\r\nUser-Agent: NTRIP RS3/1.0\r\n\r\n"
        req = parse_request(raw)
        assert req.method == "GET"
        assert req.target == "/TESTMOUNT"
        assert req.mountpoint == "TESTMOUNT"
        assert req.http_version == "HTTP/1.0"
        assert req.ntrip_version == 1
        assert req.basic_auth() is None
        assert not req.is_sourcetable_request

    def test_v2_get_mountpoint_with_basic_auth(self) -> None:
        token = base64.b64encode(b"alice:hunter2").decode("ascii")
        raw = (
            f"GET /M HTTP/1.1\r\n"
            f"Host: caster.example.com\r\n"
            f"Ntrip-Version: Ntrip/2.0\r\n"
            f"Authorization: Basic {token}\r\n"
            f"User-Agent: NTRIP TestClient/2.0\r\n"
            f"\r\n"
        ).encode("ascii")
        req = parse_request(raw)
        assert req.ntrip_version == 2
        assert req.basic_auth() == ("alice", "hunter2")

    def test_root_is_sourcetable_request(self) -> None:
        raw = b"GET / HTTP/1.1\r\nHost: x\r\n\r\n"
        req = parse_request(raw)
        assert req.is_sourcetable_request

    def test_malformed_request_line_raises(self) -> None:
        with pytest.raises(HandshakeError):
            parse_request(b"GARBAGE\r\n\r\n")

    def test_unsupported_method_raises(self) -> None:
        with pytest.raises(HandshakeError):
            parse_request(b"POST /M HTTP/1.1\r\n\r\n")

    def test_oversized_header_raises(self) -> None:
        big = b"GET /M HTTP/1.1\r\nX: " + (b"a" * 10000) + b"\r\n\r\n"
        with pytest.raises(HandshakeError):
            parse_request(big)

    def test_garbled_basic_auth_returns_none(self) -> None:
        raw = (
            b"GET /M HTTP/1.1\r\n"
            b"Authorization: Basic !!!!not-base64!!!!\r\n"
            b"\r\n"
        )
        req = parse_request(raw)
        assert req.basic_auth() is None
