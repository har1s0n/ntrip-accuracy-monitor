"""Юнит-тесты RtcmHub: fan-out, drop-on-full, unsubscribe."""

from __future__ import annotations

import asyncio

import pytest

from ntrip_accuracy_monitor.protocols.ntrip import RtcmHub


class TestRtcmHub:
    @pytest.mark.asyncio
    async def test_fanout_three_subscribers_receive_same_frames(self) -> None:
        hub = RtcmHub(subscriber_queue_size=16)
        frames = [b"frame-%d" % i for i in range(5)]

        async def collect(n: int) -> list[bytes]:
            async with hub.subscribe() as q:
                got: list[bytes] = []
                while len(got) < n:
                    item = await q.get()
                    if item is None:
                        break
                    got.append(item)
                return got

        # подписываем трёх потребителей до начала feed()
        async with asyncio.TaskGroup() as tg:
            t1 = tg.create_task(collect(5))
            t2 = tg.create_task(collect(5))
            t3 = tg.create_task(collect(5))

            # ждём подписки
            while hub.subscriber_count < 3:
                await asyncio.sleep(0)

            for f in frames:
                hub.feed(f)

        assert t1.result() == frames
        assert t2.result() == frames
        assert t3.result() == frames
        assert hub.frames_fed == 5
        assert hub.total_dropped == 0
        assert hub.subscriber_count == 0  # все вышли из контекста

    @pytest.mark.asyncio
    async def test_slow_subscriber_drops_frames_others_unaffected(self) -> None:
        hub = RtcmHub(subscriber_queue_size=2)

        slow_started = asyncio.Event()
        fast_started = asyncio.Event()
        slow_release = asyncio.Event()
        slow_dropped: asyncio.Future[int] = asyncio.get_event_loop().create_future()

        async def slow() -> None:
            async with hub.subscribe() as q:
                slow_started.set()
                await slow_release.wait()
                slow_dropped.set_result(hub.dropped_for(q))

        async def fast() -> list[bytes]:
            async with hub.subscribe() as q:
                fast_started.set()
                got: list[bytes] = []
                for _ in range(5):
                    item = await q.get()
                    assert item is not None
                    got.append(item)
                return got

        async with asyncio.TaskGroup() as tg:
            slow_task = tg.create_task(slow())
            fast_task = tg.create_task(fast())

            await slow_started.wait()
            await fast_started.wait()

            # Кормим по одному, между фреймами уступаем event loop:
            # - slow свою очередь (size=2) набирает первыми двумя, потом drop для slow.
            # - fast после каждого sleep(0) успевает прочитать один фрейм, освободив место.
            for i in range(5):
                hub.feed(b"f%d" % i)
                await asyncio.sleep(0)

            # ждём пока fast вычитает все 5 (он сам выйдет из subscribe-блока)
            result = await fast_task
            slow_release.set()
            await slow_task

        assert result == [b"f0", b"f1", b"f2", b"f3", b"f4"]
        assert slow_dropped.result() == 3
        assert hub.total_dropped == 3

    @pytest.mark.asyncio
    async def test_unsubscribe_after_context_exit(self) -> None:
        hub = RtcmHub()
        async with hub.subscribe():
            assert hub.subscriber_count == 1
        assert hub.subscriber_count == 0

    @pytest.mark.asyncio
    async def test_shutdown_sends_sentinel(self) -> None:
        hub = RtcmHub(subscriber_queue_size=8)

        async def consume() -> list[bytes | None]:
            async with hub.subscribe() as q:
                items: list[bytes | None] = []
                while True:
                    item = await q.get()
                    items.append(item)
                    if item is None:
                        return items

        async with asyncio.TaskGroup() as tg:
            t = tg.create_task(consume())
            while hub.subscriber_count < 1:
                await asyncio.sleep(0)
            hub.feed(b"x")
            hub.shutdown()

        assert t.result() == [b"x", None]
