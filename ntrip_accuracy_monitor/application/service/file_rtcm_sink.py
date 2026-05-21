"""Подписчик RtcmHub: пишет сырые байты фреймов в файл.

Один файл на сеанс: ``{captures.directory}/{session_id:06d}_{stream_id}.bin``.
Открывается на старте сеанса, закрывается на остановке. Без ротации:
RTCM 3.x на 1 Гц даёт ~7-15 МБ за 1-2 часа лабораторного прогона.

Запись — синхронный ``file.write`` на буферизированный файл. Для типичного
объёма RTCM накладные расходы пренебрежимы (микросекунды на фрейм).
Если в будущем понадобится высокочастотный поток (MSM7, RTCM4),
вынести в отдельную задачу с очередью и пакетной асинхронной записью.

Поведение при ошибке записи (диск переполнен, права):
    OSError логируется, счётчик write_failures растёт, ПОТРЕБЛЕНИЕ
    очереди продолжается — чтобы не блокировать других подписчиков
    RtcmHub. Аудит RTCM не должен падать из-за проблем с файловом.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from types import TracebackType
from typing import IO, Final, Self

logger: Final = logging.getLogger(__name__)


class FileRtcmSink:
    """Подписчик RtcmHub, пишущий сырые RTCM-байты в файл сеанса."""

    def __init__(
        self,
        *,
        directory: Path,
        session_id: int,
        stream_id: str,
    ) -> None:
        if not stream_id.strip():
            raise ValueError("stream_id must be a non-empty string")
        if session_id < 0:
            raise ValueError(f"session_id must be >= 0 (got {session_id})")
        self._directory: Final = directory
        self._session_id: Final = session_id
        self._stream_id: Final = stream_id
        self._path: Final = directory / f"{session_id:06d}_{stream_id}.bin"

        self._fh: IO[bytes] | None = None
        self._frames_written: int = 0
        self._bytes_written: int = 0
        self._write_failures: int = 0

    # ----------------------------- properties -----------------------------
    @property
    def path(self) -> Path:
        return self._path

    @property
    def frames_written(self) -> int:
        return self._frames_written

    @property
    def bytes_written(self) -> int:
        return self._bytes_written

    @property
    def write_failures(self) -> int:
        return self._write_failures

    @property
    def is_open(self) -> bool:
        return self._fh is not None

    # ------------------------- lifecycle (manual) -------------------------
    async def aopen(self) -> None:
        """Создать директорию (если нет) и открыть файл на запись."""
        if self._fh is not None:
            raise RuntimeError(f"FileRtcmSink already open: {self._path}")
        await asyncio.to_thread(
            self._directory.mkdir, parents=True, exist_ok=True,
        )
        self._fh = await asyncio.to_thread(open, self._path, "wb")
        logger.info(
            "FileRtcmSink: открыт файл %s (сеанс %d)",
            self._path, self._session_id,
        )

    async def aclose(self) -> None:
        """Закрыть файл"""
        fh = self._fh
        self._fh = None
        if fh is None:
            return
        try:
            await asyncio.to_thread(fh.close)
        except OSError as exc:
            logger.warning(
                "FileRtcmSink: ошибка при закрытии %s: %s", self._path, exc,
            )
            return
        logger.info(
            "FileRtcmSink: %s закрыт (%d фреймов, %d байт, %d ошибок записи)",
            self._path, self._frames_written, self._bytes_written,
            self._write_failures,
        )

    # ----------------------- async context manager ------------------------
    async def __aenter__(self) -> Self:
        await self.aopen()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()

    # ----------------------------- public API -----------------------------
    async def consume_hub(self, queue: asyncio.Queue[bytes | None]) -> None:
        """Главный цикл: тянет фреймы из подписки RtcmHub и пишет в файл.

        Завершается на:
          - sentinel-None из очереди (источник прекратил выдачу);
          - asyncio.CancelledError (остановка снаружи).

        В finally делается ``fh.flush()`` — буфер записи отправляется
        в ядро. Закрытие файла НЕ делается тут; за это отвечает
        ``aclose()`` / ``__aexit__``, который вызывает SessionLifecycle
        в финальном порядке остановки.
        """
        if self._fh is None:
            raise RuntimeError(
                "FileRtcmSink.consume_hub called before aopen/__aenter__",
            )
        try:
            while True:
                item = await queue.get()
                if item is None:
                    logger.info(
                        "FileRtcmSink: получен sentinel-None, выход",
                    )
                    return
                self._write_one(item)
        finally:
            fh = self._fh
            if fh is not None:
                try:
                    fh.flush()
                except OSError as exc:
                    logger.warning(
                        "FileRtcmSink: flush %s упал: %s", self._path, exc,
                    )

    # ----------------------------- internal -------------------------------
    def _write_one(self, frame: bytes) -> None:
        """Синхронная запись одного фрейма. OSError логируется и подавляется."""
        fh = self._fh
        if fh is None:  # защита от гонки с aclose — не должно случиться
            return
        try:
            fh.write(frame)
        except OSError as exc:
            self._write_failures += 1
            if self._write_failures == 1:
                logger.error(
                    "FileRtcmSink: первая ошибка записи в %s: %s "
                    "(счётчик write_failures растёт)",
                    self._path, exc,
                )
            return
        self._frames_written += 1
        self._bytes_written += len(frame)
