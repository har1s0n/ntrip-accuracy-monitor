# ntrip_accuracy_monitor/protocols/ntrip/caster.py — NtripCasterServer

"""NTRIP 1.0/2.0 кастер с одним mountpoint и Basic-auth.

Маршрутизация:
  GET /                        → sourcetable
  GET /<mountpoint>            → 401 без auth, иначе ICY/HTTP 200 + поток RTCM
  GET /<unknown_mountpoint>    → V1: sourcetable; V2: 404
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from types import TracebackType
from typing import Final, Self

from ._hub import RtcmHub
from ._server_handshake import HandshakeError, NtripRequest, read_request
from ._sourcetable import StrRecord, build_sourcetable

logger: Final = logging.getLogger(__name__)

_SERVER_AGENT: Final = "ntrip-accuracy-monitor/0.1"


class NtripCasterServer:
    """Локальный NTRIP-кастер. Один mountpoint, один источник, N клиентов."""

    def __init__(
        self,
        *,
        host: str,
        port: int,
        mountpoint: str,
        hub: RtcmHub,
        username: str | None = None,
        password: str | None = None,
        sourcetable_country: str = "POL",
        handshake_timeout_s: float = 10.0,
    ) -> None:
        self._host: Final = host
        self._port: Final = port
        self._mountpoint: Final = mountpoint
        self._hub: Final = hub
        self._username: Final = username
        self._password: Final = password
        self._handshake_timeout_s: Final = handshake_timeout_s
        self._str_record: Final = StrRecord(
            mountpoint=mountpoint,
            country=sourcetable_country,
            authentication="B" if (username or password) else "N",
        )
        self._server: asyncio.Server | None = None
        self._client_tasks: set[asyncio.Task[None]] = set()

        self._clients_accepted = 0
        self._clients_authorized = 0
        self._clients_rejected_auth = 0
        self._clients_rejected_404 = 0
        self._sourcetable_served = 0
        self._handshake_errors = 0

    # ---- public properties ----
    @property
    def clients_accepted(self) -> int:
        return self._clients_accepted

    @property
    def clients_authorized(self) -> int:
        return self._clients_authorized

    @property
    def clients_rejected_auth(self) -> int:
        return self._clients_rejected_auth

    @property
    def clients_rejected_404(self) -> int:
        return self._clients_rejected_404

    @property
    def sourcetable_served(self) -> int:
        return self._sourcetable_served

    @property
    def handshake_errors(self) -> int:
        return self._handshake_errors

    @property
    def sockets(self) -> tuple[tuple[str, int], ...]:
        if self._server is None or not self._server.sockets:
            return ()
        return tuple(s.getsockname()[:2] for s in self._server.sockets)

    @property
    def port(self) -> int:
        """Эффективный порт (для port=0 даёт реальный после start())."""
        socks = self.sockets
        if not socks:
            return self._port
        return socks[0][1]

    # ---- lifecycle ----
    async def start(self) -> None:
        if self._server is not None:
            raise RuntimeError("caster already started")
        self._server = await asyncio.start_server(
            self._handle_client, self._host, self._port,
        )
        logger.info(
            "NTRIP caster listening on %s (mountpoint=%s, auth=%s)",
            self.sockets, self._mountpoint,
            "yes" if (self._username or self._password) else "no",
        )

    async def serve_forever(self) -> None:
        if self._server is None:
            raise RuntimeError("call start() first")
        async with self._server:
            await self._server.serve_forever()

    async def aclose(self) -> None:
        for t in list(self._client_tasks):
            t.cancel()
        if self._client_tasks:
            await asyncio.gather(*self._client_tasks, return_exceptions=True)
            self._client_tasks.clear()
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    async def __aenter__(self) -> Self:
        await self.start()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()

    # ---- request handling ----
    async def _handle_client(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        self._clients_accepted += 1
        peer = writer.get_extra_info("peername")
        logger.info("Client connected from %s", peer)
        task = asyncio.current_task()
        if task is not None:
            self._client_tasks.add(task)
        try:
            try:
                request = await read_request(
                    reader, timeout_s=self._handshake_timeout_s,
                )
            except (HandshakeError, TimeoutError) as exc:
                self._handshake_errors += 1
                logger.warning("Handshake failed from %s: %s", peer, exc)
                await self._respond_400(writer)
                return
            await self._dispatch(request, reader, writer, peer)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Unhandled error in client handler %s", peer)
        finally:
            logger.info("Client disconnected: %s", peer)
            with suppress(Exception):
                writer.close()
                await writer.wait_closed()
            if task is not None:
                self._client_tasks.discard(task)

    async def _dispatch(
        self,
        request: NtripRequest,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        peer: object,
    ) -> None:
        if request.is_sourcetable_request:
            await self._respond_sourcetable(writer)
            return

        if request.mountpoint != self._mountpoint:
            self._clients_rejected_404 += 1
            if request.ntrip_version == 1:
                await self._respond_sourcetable(writer)
            else:
                await self._respond_404(writer)
            return

        if self._username is not None or self._password is not None:
            creds = request.basic_auth()
            expected = (self._username or "", self._password or "")
            if creds is None or creds != expected:
                self._clients_rejected_auth += 1
                await self._respond_401(writer)
                return

        self._clients_authorized += 1
        logger.info(
            "Client authorized: %s mountpoint=%s ntrip=%d",
            peer, request.mountpoint, request.ntrip_version,
        )
        await self._send_stream(request, reader, writer, peer)

    # ---- streaming ----
    async def _send_stream(
        self,
        request: NtripRequest,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        peer: object,
    ) -> None:
        if request.ntrip_version == 2:
            head = (
                f"HTTP/1.1 200 OK\r\n"
                f"Ntrip-Version: Ntrip/2.0\r\n"
                f"Server: {_SERVER_AGENT}\r\n"
                f"Cache-Control: no-store, no-cache, max-age=0\r\n"
                f"Pragma: no-cache\r\n"
                f"Connection: close\r\n"
                f"Content-Type: gnss/data\r\n"
                f"\r\n"
            ).encode("ascii")
        else:
            head = b"ICY 200 OK\r\n\r\n"

        writer.write(head)
        await writer.drain()

        drain_task = asyncio.create_task(self._drain_client_input(reader))

        try:
            async with self._hub.subscribe() as queue:
                while True:
                    frame = await queue.get()
                    if frame is None:  # source shutdown sentinel
                        return
                    writer.write(frame)
                    try:
                        await writer.drain()
                    except (ConnectionResetError, BrokenPipeError):
                        logger.info("Client %s disconnected during stream", peer)
                        return
        finally:
            drain_task.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await drain_task

    async def _drain_client_input(self, reader: asyncio.StreamReader) -> None:
        try:
            while True:
                data = await reader.read(4096)
                if not data:
                    return
        except (ConnectionResetError, BrokenPipeError):
            return

    # ---- canned responses ----
    async def _respond_sourcetable(self, writer: asyncio.StreamWriter) -> None:
        self._sourcetable_served += 1
        body = build_sourcetable([self._str_record])
        head = (
            f"SOURCETABLE 200 OK\r\n"
            f"Server: {_SERVER_AGENT}\r\n"
            f"Content-Type: text/plain\r\n"
            f"Content-Length: {len(body)}\r\n"
            f"Connection: close\r\n"
            f"\r\n"
        ).encode("ascii")
        writer.write(head + body)
        with suppress(Exception):
            await writer.drain()

    async def _respond_401(self, writer: asyncio.StreamWriter) -> None:
        head = (
            f"HTTP/1.1 401 Unauthorized\r\n"
            f"WWW-Authenticate: Basic realm=\"{self._mountpoint}\"\r\n"
            f"Connection: close\r\n"
            f"\r\n"
        ).encode("ascii")
        writer.write(head)
        with suppress(Exception):
            await writer.drain()

    async def _respond_404(self, writer: asyncio.StreamWriter) -> None:
        writer.write(b"HTTP/1.1 404 Not Found\r\nConnection: close\r\n\r\n")
        with suppress(Exception):
            await writer.drain()

    async def _respond_400(self, writer: asyncio.StreamWriter) -> None:
        writer.write(b"HTTP/1.1 400 Bad Request\r\nConnection: close\r\n\r\n")
        with suppress(Exception):
            await writer.drain()
