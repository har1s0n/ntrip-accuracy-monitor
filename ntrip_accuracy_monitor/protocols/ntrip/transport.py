"""Asyncio NTRIP client (own implementation, no pygnssutils).

Architecture:
  - Single ``_supervise`` task drives the connect / read / reconnect
    loop. Each iteration is one ``_run_one_session``.
  - ``_run_one_session`` opens a TCP/TLS connection with
    ``asyncio.open_connection``, sends the NTRIP handshake, parses the
    response (HTTP/1.x or ICY-style — see ``_handshake.py``), then
    streams RTCM3 frames via ``_framer.stream_rtcm_frames``.
  - Two concurrent tasks per session: a frame consumer (drains the
    framer into the public asyncio queue) and a watchdog (asserts
    progress against ``stall_timeout_s``).
  - SOURCETABLE / 401 / 404 are detected directly from the parsed
    handshake response — no log-scraping heuristics.
  - Public API is preserved 1:1 with the previous pygnssutils-backed
    implementation, plus ``crc_failures`` counter (additive).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import ssl
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from types import TracebackType
from typing import Final, Literal, Self

from ntrip_accuracy_monitor.protocols.ntrip._chunked import ChunkedReader
from ntrip_accuracy_monitor.protocols.ntrip._framer import (
    AsyncByteReader,
    stream_rtcm_frames,
)
from ntrip_accuracy_monitor.protocols.backoff import BackoffPolicy
from ntrip_accuracy_monitor.protocols.ntrip._handshake import (
    HandshakeParseError,
    NtripResponse,
    build_request,
    parse_response,
)
from ntrip_accuracy_monitor.protocols.ntrip.exceptions import (
    NtripAuthError,
    NtripMountpointError,
    NtripPermanentError,
    NtripSourcetableError,
)

_DEFAULT_USER_AGENT: Final[str] = "NTRIP ntrip-accuracy-monitor/0.1"
_MAX_PORT_NUMBER: Final[int] = 65535
_HANDSHAKE_READ_CHUNK: Final[int] = 4096
_HANDSHAKE_BUFFER_LIMIT: Final[int] = 64 * 1024  # 64 KiB cap for headers
_WATCHDOG_TICK_S: Final[float] = 0.25

type GGAProvider = Callable[[], Awaitable[bytes | None]]


class _EndSentinel:
    """Marker placed in the asyncio queue to signal end-of-stream."""


_END: Final[_EndSentinel] = _EndSentinel()


class NtripClient:
    """Asyncio NTRIP client yielding raw RTCM frames.

    Usage::

        async with NtripClient(...) as client:
            async for raw_rtcm in client:
                process(raw_rtcm)

    The client transparently reconnects on transient network failures
    using the supplied BackoffPolicy. Permanent protocol errors
    (SOURCETABLE / 401 / 404) are surfaced as NtripPermanentError from
    __anext__ and stop the supervisor without further reconnection.
    """

    GGAProvider = Callable[[], Awaitable[bytes | None]]
    """Returns one NMEA GGA sentence (with $GPGGA prefix and *XX checksum, CRLF
    optional — we add it). None means 'skip this tick, no fix yet'."""

    def __init__(
        self,
        *,
        stream_id: str,
        caster_host: str,
        caster_port: int,
        use_https: bool,
        mountpoint: str,
        username: str | None,
        password: str | None,
        ntrip_version: Literal["1.0", "2.0"],
        connect_timeout_s: float,
        stall_timeout_s: float,
        reconnect_backoff: BackoffPolicy,
        queue_max_size: int = 1024,
        user_agent: str = _DEFAULT_USER_AGENT,
        gga_provider: GGAProvider | None = None,
        gga_interval_s: float = 10.0,
    ) -> None:
        if not stream_id.strip():
            raise ValueError("stream_id must be non-empty")
        if not 1 <= caster_port <= _MAX_PORT_NUMBER:
            raise ValueError(f"caster_port out of range: {caster_port}")
        if not mountpoint:
            raise ValueError("mountpoint must be non-empty")
        if connect_timeout_s <= 0:
            raise ValueError(f"connect_timeout_s must be > 0 (got {connect_timeout_s})")
        if stall_timeout_s <= 0:
            raise ValueError(f"stall_timeout_s must be > 0 (got {stall_timeout_s})")
        if queue_max_size < 1:
            raise ValueError(f"queue_max_size must be >= 1 (got {queue_max_size})")

        if gga_interval_s <= 0:
            raise ValueError(
                f"gga_interval_s must be > 0 (got {gga_interval_s})"
            )

        self._stream_id = stream_id
        self._caster_host = caster_host
        self._caster_port = caster_port
        self._use_https = use_https
        self._mountpoint = mountpoint
        self._username = username
        self._password = password
        self._ntrip_version: Literal["1.0", "2.0"] = ntrip_version
        self._connect_timeout_s = connect_timeout_s
        self._stall_timeout_s = stall_timeout_s
        self._backoff = reconnect_backoff
        self._user_agent = user_agent

        self._logger: logging.LoggerAdapter[logging.Logger] = logging.LoggerAdapter(
            logging.getLogger("ntrip_accuracy_monitor.protocols.ntrip.client"),
            {"stream_id": stream_id},
        )

        # Lifecycle / state — single-threaded, event-loop only.
        self._aqueue: asyncio.Queue[bytes | _EndSentinel] = asyncio.Queue(
            maxsize=queue_max_size
        )
        self._supervisor_task: asyncio.Task[None] | None = None
        self._closed: bool = False
        self._fatal_error: NtripPermanentError | None = None

        # Counters.
        self._frames_received: int = 0
        self._reconnects: int = 0
        self._stall_timeouts: int = 0
        self._dropped_full: int = 0
        self._crc_failures: int = 0
        self._connect_attempt: int = 0
        self._last_frame_at: float = 0.0  # monotonic seconds

        self._gga_provider = gga_provider
        self._gga_interval_s = gga_interval_s
        self._gga_sent: int = 0

    # ----------------------------- properties -----------------------------
    @property
    def stream_id(self) -> str:
        return self._stream_id

    @property
    def fatal_error(self) -> NtripPermanentError | None:
        return self._fatal_error

    @property
    def frames_received(self) -> int:
        return self._frames_received

    @property
    def reconnects(self) -> int:
        return self._reconnects

    @property
    def stall_timeouts(self) -> int:
        return self._stall_timeouts

    @property
    def dropped_full(self) -> int:
        return self._dropped_full

    @property
    def crc_failures(self) -> int:
        return self._crc_failures

    @property
    def gga_sent(self) -> int:
        return self._gga_sent

    # ----------------------- async context / iterator ---------------------
    async def __aenter__(self) -> Self:
        if self._supervisor_task is not None:
            raise RuntimeError("NtripClient already started")
        self._supervisor_task = asyncio.create_task(
            self._supervise(), name=f"ntrip-supervisor-{self._stream_id}"
        )
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()

    def __aiter__(self) -> Self:
        return self

    async def __anext__(self) -> bytes:
        item = await self._aqueue.get()
        if isinstance(item, _EndSentinel):
            if self._fatal_error is not None:
                raise self._fatal_error
            raise StopAsyncIteration
        return item

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        task = self._supervisor_task
        self._supervisor_task = None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self._signal_end()

    # ----------------------------- supervisor -----------------------------
    async def _supervise(self) -> None:
        try:
            while True:
                self._connect_attempt += 1
                self._logger.info(
                    "ntrip connect attempt %d to %s://%s:%d/%s",
                    self._connect_attempt,
                    "https" if self._use_https else "http",
                    self._caster_host,
                    self._caster_port,
                    self._mountpoint,
                )
                fatal = await self._run_one_session()
                if self._closed:
                    return
                if fatal is not None:
                    self._fatal_error = fatal
                    self._logger.error("ntrip fatal: %s", fatal)
                    return
                self._reconnects += 1
                delay = self._backoff.delay_for_attempt(self._connect_attempt)
                self._logger.warning(
                    "ntrip session ended; reconnect in %.2fs (attempt=%d)",
                    delay,
                    self._connect_attempt,
                )
                await asyncio.sleep(delay)
        except asyncio.CancelledError:
            self._logger.info("ntrip supervisor cancelled")
            raise
        finally:
            self._signal_end()

    def _signal_end(self) -> None:
        try:
            self._aqueue.put_nowait(_END)
            return
        except asyncio.QueueFull:
            pass
        try:
            self._aqueue.get_nowait()
        except asyncio.QueueEmpty:
            return
        with contextlib.suppress(asyncio.QueueFull):
            self._aqueue.put_nowait(_END)

    # ----------------------------- one session ----------------------------
    async def _run_one_session(self) -> NtripPermanentError | None:
        """Run a single connect + handshake + stream cycle.

        Returns:
            NtripPermanentError on auth/mountpoint/sourcetable failure
            (caller stops the supervisor). ``None`` on transient failure
            or normal close (caller backs off and retries).
        """
        ssl_ctx: ssl.SSLContext | None = (
            ssl.create_default_context() if self._use_https else None
        )

        reader: asyncio.StreamReader | None = None
        writer: asyncio.StreamWriter | None = None
        try:
            try:
                reader, writer = await asyncio.wait_for(
                    asyncio.open_connection(
                        self._caster_host,
                        self._caster_port,
                        ssl=ssl_ctx,
                    ),
                    timeout=self._connect_timeout_s,
                )
            except (TimeoutError, asyncio.TimeoutError):
                self._logger.warning(
                    "ntrip connect timeout after %.1fs", self._connect_timeout_s
                )
                return None
            except OSError as exc:
                self._logger.warning("ntrip connect error: %s", exc)
                return None

            request = build_request(
                host=self._caster_host,
                port=self._caster_port,
                mountpoint=self._mountpoint,
                username=self._username,
                password=self._password,
                ntrip_version=self._ntrip_version,
                user_agent=self._user_agent,
            )
            self._logger.debug(
                "ntrip request bytes (%d):\n%s",
                len(request),
                request.decode("ascii", errors="backslashreplace"),
            )
            writer.write(request)
            try:
                await asyncio.wait_for(writer.drain(), timeout=self._connect_timeout_s)
            except (TimeoutError, asyncio.TimeoutError):
                self._logger.warning(
                    "ntrip request send timeout after %.1fs", self._connect_timeout_s
                )
                return None

            # Gate GGA uplink on receipt of the first response byte.
            # Some casters (EFT RS3 with GGA-switching ON) silently drop
            # any payload that arrives in the same TCP segment as the
            # trailing \r\n\r\n of the GET request — they finish parsing
            # the request, then look at the buffer fresh on the next
            # recv(). Sending GGA only after we've seen a byte back
            # guarantees a separate recv() on the caster side.
            handshake_initial: bytes = b""
            if self._gga_provider is not None:
                try:
                    handshake_initial = await asyncio.wait_for(
                        self._wait_first_response_byte(reader),
                        timeout=self._connect_timeout_s,
                    )
                except (TimeoutError, asyncio.TimeoutError):
                    self._logger.warning(
                        "ntrip waiting for first response byte timed out "
                        "after %.1fs", self._connect_timeout_s,
                    )
                    return None
                except OSError as exc:
                    self._logger.warning(
                        "ntrip first-byte read error: %s", exc
                    )
                    return None

            uplink_task: asyncio.Task[None] | None = None
            if self._gga_provider is not None:
                uplink_task = asyncio.create_task(
                    self._gga_uplink_loop(writer), name="ntrip-gga"
                )

            try:
                try:
                    response = await asyncio.wait_for(
                        self._read_handshake(
                            reader, initial_buffer=handshake_initial,
                        ),
                        timeout=self._connect_timeout_s,
                    )
                except (TimeoutError, asyncio.TimeoutError):
                    self._logger.warning(
                        "ntrip handshake timeout after %.1fs",
                        self._connect_timeout_s,
                    )
                    return None
                except HandshakeParseError as exc:
                    self._logger.warning("ntrip handshake parse error: %s", exc)
                    return None
                except OSError as exc:
                    self._logger.warning("ntrip handshake read error: %s", exc)
                    return None

                fatal = self._classify_response(response)
                if fatal is not None:
                    return fatal
                if response.status_code != 200:
                    self._logger.warning(
                        "ntrip unexpected status %d %s; treating as transient",
                        response.status_code,
                        response.status_reason,
                    )
                    return None

                self._logger.info(
                    "ntrip streaming RTCM3 from %s:%d/%s (proto=%s)",
                    self._caster_host, self._caster_port, self._mountpoint,
                    response.protocol,
                )
                self._last_frame_at = time.monotonic()

                return await self._consume_stream(
                    reader, response.leftover, response.headers,
                )
            finally:
                if uplink_task is not None:
                    uplink_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError, Exception):
                        await uplink_task

        except asyncio.CancelledError:
            raise
        finally:
            if writer is not None:
                with contextlib.suppress(Exception):
                    writer.close()
                with contextlib.suppress(Exception):
                    await writer.wait_closed()

    async def _wait_first_response_byte(
        self, reader: asyncio.StreamReader,
    ) -> bytes:
        """Block until the caster sends at least one byte, return it.

        Used to gate GGA uplink on casters (e.g. EFT RS3) that drop any
        bytes arriving in the same recv() as the trailing \\r\\n\\r\\n of
        the GET request. Returning the byte allows the handshake reader
        to consume it from a passed-in initial buffer.
        """
        first = await reader.read(1)
        if not first:
            raise OSError("connection closed before any response byte")
        return first

    async def _read_handshake(
        self,
        reader: asyncio.StreamReader,
        *,
        initial_buffer: bytes = b"",
    ) -> NtripResponse:
        """Read from socket until parse_response yields a complete response."""
        buf = bytearray(initial_buffer)
        chunks_seen = 0
        # If the initial buffer already contains a parseable response
        # (small header from a fast caster might fit in one byte? no —
        # but for symmetry / future-proof) check before reading more.
        if buf:
            response = parse_response(bytes(buf))
            if response is not None:
                return response
        while True:
            chunk = await reader.read(_HANDSHAKE_READ_CHUNK)
            chunks_seen += 1
            self._logger.debug(
                "ntrip handshake recv chunk #%d (%d bytes): %r",
                chunks_seen, len(chunk), chunk[:256],
            )
            if not chunk:
                raise OSError(
                    f"connection closed during handshake "
                    f"(received {len(buf)} bytes total in {chunks_seen - 1} chunks)"
                )
            buf.extend(chunk)
            if len(buf) > _HANDSHAKE_BUFFER_LIMIT:
                raise HandshakeParseError(
                    f"handshake header exceeded {_HANDSHAKE_BUFFER_LIMIT} bytes"
                )
            response = parse_response(bytes(buf))
            if response is not None:
                return response

    def _classify_response(
        self, response: NtripResponse,
    ) -> NtripPermanentError | None:
        if response.protocol.upper() == "SOURCETABLE":
            return NtripSourcetableError(
                f"caster returned sourcetable instead of stream "
                f"for mountpoint {self._mountpoint!r}"
            )
        if response.status_code == 401:
            return NtripAuthError(
                f"caster returned 401 {response.status_reason}"
            )
        if response.status_code == 404:
            return NtripMountpointError(
                f"mountpoint {self._mountpoint!r} not found "
                f"(404 {response.status_reason})"
            )
        return None

    async def _gga_uplink_loop(self, writer: asyncio.StreamWriter) -> None:
        """Send GGA sentences to the caster at gga_interval_s.

        Started BEFORE handshake read because some casters (EFT RS3 with
        GGA-switching enabled) won't end the response header block until
        the client has sent a position fix in the same TCP session.
        First GGA goes out immediately.
        """
        assert self._gga_provider is not None
        try:
            while True:
                try:
                    sentence = await self._gga_provider()
                except Exception as exc:  # noqa: BLE001
                    self._logger.warning(
                        "ntrip GGA provider raised %s: %s",
                        type(exc).__name__, exc,
                    )
                    return
                if sentence:
                    try:
                        writer.write(sentence)
                        await writer.drain()
                        self._gga_sent += 1
                        self._logger.debug(
                            "ntrip GGA sent (%d bytes)", len(sentence)
                        )
                    except (ConnectionError, OSError) as exc:
                        self._logger.warning(
                            "ntrip GGA uplink write failed: %s", exc
                        )
                        return
                await asyncio.sleep(self._gga_interval_s)
        except asyncio.CancelledError:
            raise

    async def _consume_stream(
        self,
        reader: asyncio.StreamReader,
        leftover: bytes,
        headers: dict[str, str],
    ) -> NtripPermanentError | None:
        """Run framer + watchdog. Returns None on any transient end."""
        body_reader: AsyncByteReader
        framer_initial: bytes
        te = headers.get("transfer-encoding", "").lower()
        if "chunked" in te:
            body_reader = ChunkedReader(reader, initial_buffer=leftover)
            framer_initial = b""
            self._logger.debug("ntrip body framing: chunked")
        else:
            body_reader = reader
            framer_initial = leftover
            self._logger.debug("ntrip body framing: raw")

        def on_resync(discarded: bytes) -> None:
            if discarded:
                self._crc_failures += 1

        async def consume() -> None:
            async for frame in stream_rtcm_frames(
                body_reader, initial_buffer=framer_initial, on_resync=on_resync,
            ):
                self._frames_received += 1
                self._last_frame_at = time.monotonic()
                if self._connect_attempt != 0:
                    self._connect_attempt = 0
                try:
                    self._aqueue.put_nowait(frame)
                except asyncio.QueueFull:
                    self._dropped_full += 1

        async def watchdog() -> None:
            while True:
                await asyncio.sleep(_WATCHDOG_TICK_S)
                if time.monotonic() - self._last_frame_at > self._stall_timeout_s:
                    self._stall_timeouts += 1
                    self._logger.warning(
                        "ntrip stall: no frame for %.1fs", self._stall_timeout_s
                    )
                    return

        consume_task = asyncio.create_task(consume(), name="ntrip-consume")
        watchdog_task = asyncio.create_task(watchdog(), name="ntrip-watchdog")
        try:
            done, _pending = await asyncio.wait(
                {consume_task, watchdog_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in (consume_task, watchdog_task):
                if not task.done():
                    task.cancel()
                    with contextlib.suppress(asyncio.CancelledError, Exception):
                        await task
            for task in done:
                exc = task.exception()
                if exc is None or isinstance(exc, asyncio.CancelledError):
                    continue
                self._logger.warning(
                    "ntrip stream task %s raised %s: %s",
                    task.get_name(), type(exc).__name__, exc,
                )
            return None
        except asyncio.CancelledError:
            for task in (consume_task, watchdog_task):
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
            raise


# Public re-export hint: the module name and symbol stay the same so
# ``from ntrip_accuracy_monitor.protocols.ntrip.transport import NtripClient``
# keeps working without changes elsewhere.
__all__ = ["NtripClient"]
