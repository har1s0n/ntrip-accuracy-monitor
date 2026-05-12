"""Раздача RTCM3-фреймов от одного источника к множеству подписчиков.

Каждый подписчик имеет персональную asyncio.Queue. Медленный потребитель
не блокирует апстрим: при QueueFull фрейм отбрасывается с инкрементом
счетчика.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Final

logger: Final = logging.getLogger(__name__)


class RtcmHub:
    """Async раздача для RTCM3-байтовых фреймов."""

    def __init__(self, *, subscriber_queue_size: int = 256) -> None:
        self._queue_size: Final = subscriber_queue_size
        self._subscribers: set[asyncio.Queue[bytes | None]] = set()
        self._dropped: dict[int, int] = {}
        self._total_dropped: int = 0
        self._frames_fed = 0

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)

    @property
    def frames_fed(self) -> int:
        return self._frames_fed

    @property
    def total_dropped(self) -> int:
        return self._total_dropped

    def dropped_for(self, queue: asyncio.Queue[bytes | None]) -> int:
        """Количество фреймов, отброшенных конкретно этим подписчиком."""
        return self._dropped.get(id(queue), 0)

    def feed(self, frame: bytes) -> None:
        """Положить фрейм во все очереди подписчиков. Никогда не блокирует.

        Однопоточный asyncio: между put_nowait вызовами нет await,
        поэтому изменение _subscribers/_dropped безопасно.
        """
        self._frames_fed += 1
        for q in self._subscribers:
            try:
                q.put_nowait(frame)
            except asyncio.QueueFull:
                self._dropped[id(q)] = self._dropped.get(id(q), 0) + 1
                self._total_dropped += 1
                logger.warning(
                    "RtcmHub: subscriber queue full (id=%s, dropped=%d)",
                    id(q), self._dropped[id(q)],
                )

    @asynccontextmanager
    async def subscribe(self) -> AsyncIterator[asyncio.Queue[bytes | None]]:
        """Подписаться. Yields очередь, в которую feed() кладёт фреймы.

        На выходе из with-блока подписчик удаляется из набора, его счетчик
        дропов очищается.
        """
        q: asyncio.Queue[bytes | None] = asyncio.Queue(maxsize=self._queue_size)
        self._subscribers.add(q)
        self._dropped[id(q)] = 0
        try:
            yield q
        finally:
            self._subscribers.discard(q)
            self._dropped.pop(id(q), None)

    def shutdown(self) -> None:
        """Послать всем подписчикам sentinel-None.

        Используется когда апстрим (RtcmSource) остановился, но
        каст-сервер еще работает. Если очередь подписчика полна, то подписчик в любом случае
        будет отменен через cancel при aclose() сервера.
        """
        for q in self._subscribers:
            try:
                q.put_nowait(None)
            except asyncio.QueueFull:
                pass
