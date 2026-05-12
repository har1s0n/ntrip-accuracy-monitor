"""Источники RTCM3-фреймов для подачи в RtcmHub.

NtripClient из transport.py уже структурно соответствует Protocol RtcmSource

TcpRtcmSource — для случая, когда RS3 #1 в режиме "TCP/IP Server"
(тип данных RTCM3.x), без NTRIP-handshake.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import suppress
from typing import Final, Protocol, runtime_checkable

from ._framer import stream_rtcm_frames
from ._hub import RtcmHub

logger: Final = logging.getLogger(__name__)


@runtime_checkable
class RtcmSource(Protocol):
    """Любой источник CRC-валидных RTCM3-фреймов.

    NtripClient (protocols/ntrip/transport.py) и TcpRtcmSource оба
    удовлетворяют этому протоколу.
    """

    def __aiter__(self) -> AsyncIterator[bytes]: ...

    async def aclose(self) -> None: ...


class TcpRtcmSource:
    """Прямой TCP-источник RTCM3 (RS3 #1 в режиме TCP/IP Server, тип RTCM3).

    Цикл: connect → framer до EOF/ошибки → backoff → reconnect.
    Останавливается только через aclose() извне.
    """

    def __init__(
        self,
        host: str,
        port: int,
        *,
        connect_timeout_s: float = 10.0,
        reconnect_backoff_s: float = 5.0,
    ) -> None:
        self._host: Final = host
        self._port: Final = port
        self._connect_timeout_s: Final = connect_timeout_s
        self._reconnect_backoff_s: Final = reconnect_backoff_s
        self._writer: asyncio.StreamWriter | None = None
        self._closed = False
        # счётчики
        self._frames_received = 0
        self._reconnects = 0
        self._bytes_dropped = 0  # из on_resync framer'а: мусор + последствия CRC-fail

    @property
    def frames_received(self) -> int:
        return self._frames_received

    @property
    def reconnects(self) -> int:
        return self._reconnects

    @property
    def bytes_dropped(self) -> int:
        """Байты, отброшенные framer'ом из-за рассинхронизации/CRC-fail."""
        return self._bytes_dropped

    def __aiter__(self) -> AsyncIterator[bytes]:
        return self._iterate()

    async def _iterate(self) -> AsyncIterator[bytes]:
        while not self._closed:
            try:
                async with asyncio.timeout(self._connect_timeout_s):
                    reader, writer = await asyncio.open_connection(
                        self._host, self._port,
                    )
            except (OSError, TimeoutError) as exc:
                if self._closed:
                    return
                logger.warning(
                    "TcpRtcmSource connect to %s:%d failed: %s; retry in %.1fs",
                    self._host, self._port, exc, self._reconnect_backoff_s,
                )
                self._reconnects += 1
                await asyncio.sleep(self._reconnect_backoff_s)
                continue

            self._writer = writer
            logger.info(
                "TcpRtcmSource connected to %s:%d", self._host, self._port,
            )
            saw_eof_cleanly = False
            try:
                async for frame in stream_rtcm_frames(
                    reader, on_resync=self._on_resync,
                ):
                    self._frames_received += 1
                    yield frame
                # Сюда — только если framer вышел по EOF (read() == b"").
                saw_eof_cleanly = True
            except OSError as exc:
                # Сетевая ошибка во время чтения. EOF выше уже отработан.
                if self._closed:
                    return
                logger.warning("TcpRtcmSource read error: %s", exc)
            except asyncio.CancelledError:
                raise
            finally:
                with suppress(Exception):
                    writer.close()
                    await writer.wait_closed()
                self._writer = None

            if self._closed:
                return

            # EOF или ошибка чтения
            self._reconnects += 1
            log_fn = logger.info if saw_eof_cleanly else logger.warning
            log_fn(
                "TcpRtcmSource: upstream EOF/disconnect, reconnect in %.1fs",
                self._reconnect_backoff_s,
            )
            await asyncio.sleep(self._reconnect_backoff_s)

    def _on_resync(self, dropped: bytes) -> None:
        """Callback из framer при отбрасывании байтов."""
        self._bytes_dropped += len(dropped)

    async def aclose(self) -> None:
        self._closed = True
        if self._writer is not None:
            with suppress(Exception):
                self._writer.close()
                await self._writer.wait_closed()
            self._writer = None


async def pump(source: RtcmSource, hub: RtcmHub) -> None:
    """Перекачивать фреймы из источника в Hub до отмены

    На отмене корректно сигналит hub (sentinel-None всем подписчикам)
    и пробрасывает CancelledError выше.
    """
    try:
        async for frame in source:
            hub.feed(frame)
    except asyncio.CancelledError:
        raise
    finally:
        hub.shutdown()
