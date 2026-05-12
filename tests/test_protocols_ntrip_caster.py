"""Loopback-тесты NtripCasterServer + RtcmHub.

Без моков: поднимаем настоящий сервер на эфемерном порту, ходим клиентами
через asyncio.open_connection.
"""

from __future__ import annotations

import asyncio
import base64

import pytest

from ntrip_accuracy_monitor.protocols.ntrip import NtripCasterServer, RtcmHub
from contextlib import suppress


@pytest.fixture
async def hub() -> RtcmHub:
    return RtcmHub(subscriber_queue_size=64)


async def _read_until(reader: asyncio.StreamReader, marker: bytes, limit: int = 16384) -> bytes:
    buf = b""
    while marker not in buf:
        chunk = await reader.read(1024)
        if not chunk:
            break
        buf += chunk
        if len(buf) > limit:
            raise AssertionError(f"marker {marker!r} not found within {limit} bytes")
    return buf


@pytest.mark.asyncio
class TestCasterRouting:
    async def test_sourcetable_on_root(self, hub: RtcmHub) -> None:
        async with NtripCasterServer(
            host="127.0.0.1", port=0, mountpoint="TESTMOUNT", hub=hub,
        ) as caster:
            host, port = caster.sockets[0]
            r, w = await asyncio.open_connection(host, port)
            try:
                w.write(b"GET / HTTP/1.0\r\n\r\n")
                await w.drain()
                resp = await _read_until(r, b"ENDSOURCETABLE\r\n")
            finally:
                w.close()
                await w.wait_closed()
        assert resp.startswith(b"SOURCETABLE 200 OK\r\n")
        assert b"TESTMOUNT" in resp
        assert resp.rstrip().endswith(b"ENDSOURCETABLE")
        assert caster.sourcetable_served == 1

    async def test_unknown_mountpoint_v1_returns_sourcetable(self, hub: RtcmHub) -> None:
        async with NtripCasterServer(
            host="127.0.0.1", port=0, mountpoint="REAL", hub=hub,
        ) as caster:
            host, port = caster.sockets[0]
            r, w = await asyncio.open_connection(host, port)
            try:
                w.write(b"GET /WRONG HTTP/1.0\r\n\r\n")
                await w.drain()
                resp = await _read_until(r, b"ENDSOURCETABLE\r\n")
            finally:
                w.close()
                await w.wait_closed()
        assert resp.startswith(b"SOURCETABLE 200 OK")
        assert caster.clients_rejected_404 == 1

    async def test_unknown_mountpoint_v2_returns_404(self, hub: RtcmHub) -> None:
        async with NtripCasterServer(
            host="127.0.0.1", port=0, mountpoint="REAL", hub=hub,
        ) as caster:
            host, port = caster.sockets[0]
            r, w = await asyncio.open_connection(host, port)
            try:
                w.write(
                    b"GET /WRONG HTTP/1.1\r\n"
                    b"Ntrip-Version: Ntrip/2.0\r\n"
                    b"\r\n"
                )
                await w.drain()
                resp = await r.read(256)
            finally:
                w.close()
                await w.wait_closed()
        assert resp.startswith(b"HTTP/1.1 404")

    async def test_401_without_auth(self, hub: RtcmHub) -> None:
        async with NtripCasterServer(
            host="127.0.0.1", port=0, mountpoint="M", hub=hub,
            username="u", password="p",
        ) as caster:
            host, port = caster.sockets[0]
            r, w = await asyncio.open_connection(host, port)
            try:
                w.write(b"GET /M HTTP/1.0\r\n\r\n")
                await w.drain()
                resp = await r.read(512)
            finally:
                w.close()
                await w.wait_closed()
        assert resp.startswith(b"HTTP/1.1 401")
        assert b"WWW-Authenticate" in resp
        assert caster.clients_rejected_auth == 1


@pytest.mark.asyncio
class TestCasterStreaming:
    async def test_v1_icy_handshake_and_frame_delivery(self, hub: RtcmHub) -> None:
        async with NtripCasterServer(
            host="127.0.0.1", port=0, mountpoint="M", hub=hub,
        ) as caster:
            host, port = caster.sockets[0]
            r, w = await asyncio.open_connection(host, port)
            try:
                w.write(b"GET /M HTTP/1.0\r\n\r\n")
                await w.drain()

                # ждём ICY 200 OK\r\n\r\n
                head = await r.readuntil(b"\r\n\r\n")
                assert head == b"ICY 200 OK\r\n\r\n"

                # ждём подписки (caster уже её сделал к моменту чтения header)
                while hub.subscriber_count < 1:
                    await asyncio.sleep(0)

                # фидим один синтетический фрейм
                frame = b"\xd3\x00\x02PAYLOAD"
                hub.feed(frame)
                got = await asyncio.wait_for(r.readexactly(len(frame)), timeout=1.0)
            finally:
                w.close()
                await w.wait_closed()
        assert got == frame
        assert caster.clients_authorized == 1

    async def test_v2_http_handshake(self, hub: RtcmHub) -> None:
        async with NtripCasterServer(
            host="127.0.0.1", port=0, mountpoint="M", hub=hub,
        ) as caster:
            host, port = caster.sockets[0]
            r, w = await asyncio.open_connection(host, port)
            try:
                w.write(
                    b"GET /M HTTP/1.1\r\n"
                    b"Ntrip-Version: Ntrip/2.0\r\n"
                    b"\r\n"
                )
                await w.drain()
                head = await r.readuntil(b"\r\n\r\n")
                assert head.startswith(b"HTTP/1.1 200 OK")
                assert b"Ntrip-Version: Ntrip/2.0" in head
                assert b"Content-Type: gnss/data" in head
            finally:
                w.close()
                await w.wait_closed()

    async def test_three_clients_receive_same_frames(self, hub: RtcmHub) -> None:
        frame = b"\xd3FRAME"

        async with NtripCasterServer(
            host="127.0.0.1", port=0, mountpoint="M", hub=hub,
        ) as caster:
            host, port = caster.sockets[0]

            async def consume() -> bytes:
                r, w = await asyncio.open_connection(host, port)
                try:
                    w.write(b"GET /M HTTP/1.0\r\n\r\n")
                    await w.drain()
                    await r.readuntil(b"\r\n\r\n")
                    return await asyncio.wait_for(
                        r.readexactly(len(frame)), timeout=1.0,
                    )
                finally:
                    w.close()
                    await w.wait_closed()

            async with asyncio.TaskGroup() as tg:
                t1 = tg.create_task(consume())
                t2 = tg.create_task(consume())
                t3 = tg.create_task(consume())

                while hub.subscriber_count < 3:
                    await asyncio.sleep(0.01)

                hub.feed(frame)

        assert t1.result() == frame
        assert t2.result() == frame
        assert t3.result() == frame
        assert caster.clients_authorized == 3

    async def test_cancel_caster_disconnects_clients(self, hub: RtcmHub) -> None:
        caster = NtripCasterServer(
            host="127.0.0.1", port=0, mountpoint="M", hub=hub,
        )
        await caster.start()
        host, port = caster.sockets[0]
        r, w = await asyncio.open_connection(host, port)
        try:
            w.write(b"GET /M HTTP/1.0\r\n\r\n")
            await w.drain()
            await r.readuntil(b"\r\n\r\n")
            while hub.subscriber_count < 1:
                await asyncio.sleep(0)

            await caster.aclose()

            # сервер закрылся — клиент получит EOF
            tail = await asyncio.wait_for(r.read(), timeout=1.0)
            assert tail == b""
        finally:
            w.close()
            with suppress(Exception):
                await w.wait_closed()
        assert hub.subscriber_count == 0
