"""FileRtcmSource — RtcmSource поверх файла с записанным потоком RTCM 3.x.

Контракт соответствует Protocol RtcmSource из protocols/ntrip/_rtcm_source:
синхронный __aiter__, отдающий AsyncIterator[bytes] с CRC-валидными
кадрами, и async aclose() для остановки.

Файл читается целиком в память и пропускается через тот же
stream_rtcm_frames, что и сетевые источники. Это даёт идентичную
семантику отбраковки: невалидные байты, попавшие между кадрами
(например, RTCM 2.x в смешанной записи или мусор из обрыва соединения),
сбрасываются через on_resync и учитываются в bytes_dropped.

Источник одноразовый: после исчерпания файла итерация завершается.∆
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Final

from ntrip_accuracy_monitor.protocols.ntrip._framer import stream_rtcm_frames

logger: Final = logging.getLogger(__name__)

# Лимит буфера StreamReader. 16 МиБ с большим запасом покрывает любые
# разумные лабораторные захваты (наблюдаемые размеры — сотни КБ).
_READER_BUFFER_LIMIT: Final = 2 ** 24


class FileRtcmSource:
    """Файловый источник CRC-валидных RTCM 3.x кадров.

    Args:
        path: путь к файлу с записанным RTCM-потоком (сырые байты).
    """

    def __init__(self, path: Path) -> None:
        self._path: Final = path
        self._closed = False
        self._frames_received = 0
        self._bytes_dropped = 0

    @property
    def frames_received(self) -> int:
        """Число валидных RTCM 3.x кадров, отданных наружу."""
        return self._frames_received

    @property
    def bytes_dropped(self) -> int:
        """Байты, отброшенные framer'ом из-за рассинхронизации или CRC-fail."""
        return self._bytes_dropped

    def __aiter__(self) -> AsyncIterator[bytes]:
        return self._iterate()

    async def _iterate(self) -> AsyncIterator[bytes]:
        reader = asyncio.StreamReader(limit=_READER_BUFFER_LIMIT)
        try:
            data = await asyncio.to_thread(self._path.read_bytes)
        except OSError as exc:
            logger.error(
                "FileRtcmSource: не удалось прочитать %s: %s", self._path, exc,
            )
            return

        logger.info(
            "FileRtcmSource: загружено %d байт из %s", len(data), self._path,
        )
        reader.feed_data(data)
        reader.feed_eof()

        try:
            async for frame in stream_rtcm_frames(reader, on_resync=self._on_resync):
                if self._closed:
                    return
                self._frames_received += 1
                yield frame
        except asyncio.CancelledError:
            raise

        logger.info(
            "FileRtcmSource %s: отдано %d кадров, отброшено %d байт",
            self._path, self._frames_received, self._bytes_dropped,
        )

    def _on_resync(self, dropped: bytes) -> None:
        """Callback из framer при отбрасывании байтов."""
        self._bytes_dropped += len(dropped)

    async def aclose(self) -> None:
        """Запросить остановку итерации."""
        self._closed = True
