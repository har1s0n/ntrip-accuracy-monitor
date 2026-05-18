"""Юнит-тесты EpochBatchWriter (без реальной БД, на поддельном репозитории)."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

import asyncpg
import pytest

from ntrip_accuracy_monitor.application.aggregation import EpochBatchWriter
from ntrip_accuracy_monitor.domain.epoch import Epoch
from ntrip_accuracy_monitor.domain.position import GeodeticPosition
from ntrip_accuracy_monitor.domain.solution_mode import SolutionMode


def _make_epoch(stream_id: str, second: int) -> Epoch:
    return Epoch(
        epoch_time=datetime(2026, 5, 16, 12, 0, 0, tzinfo=UTC)
        + timedelta(seconds=second),
        stream_id=stream_id,
        position=GeodeticPosition(
            latitude_deg=55.0,
            longitude_deg=37.0,
            ellipsoidal_height_m=200.0,
        ),
        solution_mode=SolutionMode.RTK_FIXED,
        age_of_corrections_s=1.0,
        satellites_used=18,
        hdop=0.8,
        pdop=1.4,
        sigma_east_m=0.01,
        sigma_north_m=0.01,
        sigma_up_m=0.02,
    )


class _FakeRepo:
    """Поддельный EpochRepository, копит вставки в памяти."""

    def __init__(self) -> None:
        self.calls: list[tuple[int, list[Epoch]]] = []
        self.fail_times: int = 0
        self.fail_exception: BaseException = ConnectionError("no db")

    async def insert_batch(
        self, session_id: int, epochs: Sequence[Epoch]
    ) -> None:
        if self.fail_times > 0:
            self.fail_times -= 1
            raise self.fail_exception
        self.calls.append((session_id, list(epochs)))


@pytest.mark.asyncio
async def test_submit_below_threshold_does_not_flush() -> None:
    repo = _FakeRepo()
    w = EpochBatchWriter(
        repo,  # type: ignore[arg-type]
        session_id_provider=lambda: 7,
        max_buffer_size=5,
    )
    for i in range(4):
        await w.submit(_make_epoch("rover_rtk", i))
    assert repo.calls == []
    assert w.buffer_size == 4


@pytest.mark.asyncio
async def test_submit_triggers_flush_at_threshold() -> None:
    repo = _FakeRepo()
    w = EpochBatchWriter(
        repo,  # type: ignore[arg-type]
        session_id_provider=lambda: 7,
        max_buffer_size=5,
    )
    for i in range(5):
        await w.submit(_make_epoch("rover_rtk", i))
    assert len(repo.calls) == 1
    session_id, written = repo.calls[0]
    assert session_id == 7
    assert len(written) == 5
    assert w.buffer_size == 0
    assert w.written_total == 5


@pytest.mark.asyncio
async def test_flush_dedupes_by_stream_id_and_epoch_time() -> None:
    repo = _FakeRepo()
    w = EpochBatchWriter(
        repo,  # type: ignore[arg-type]
        session_id_provider=lambda: 7,
        max_buffer_size=100,
    )
    await w.submit(_make_epoch("rover_rtk", 0))
    await w.submit(_make_epoch("rover_rtk", 0))  # дубликат
    await w.submit(_make_epoch("rover_rtk", 1))
    await w.flush()
    assert len(repo.calls) == 1
    _, written = repo.calls[0]
    assert len(written) == 2


@pytest.mark.asyncio
async def test_flush_noop_when_session_id_none() -> None:
    repo = _FakeRepo()
    w = EpochBatchWriter(
        repo,  # type: ignore[arg-type]
        session_id_provider=lambda: None,
        max_buffer_size=100,
    )
    await w.submit(_make_epoch("rover_rtk", 0))
    await w.flush()
    assert repo.calls == []
    assert w.buffer_size == 1


@pytest.mark.asyncio
async def test_drop_oldest_when_no_session_and_buffer_full() -> None:
    repo = _FakeRepo()
    w = EpochBatchWriter(
        repo,  # type: ignore[arg-type]
        session_id_provider=lambda: None,
        max_buffer_size=3,
    )
    for i in range(5):
        await w.submit(_make_epoch("rover_rtk", i))
    assert w.buffer_size == 3
    assert w.dropped_no_session == 2


@pytest.mark.asyncio
async def test_retry_on_transient_db_error_then_success() -> None:
    repo = _FakeRepo()
    repo.fail_times = 2
    repo.fail_exception = asyncpg.InterfaceError("connection lost")
    w = EpochBatchWriter(
        repo,  # type: ignore[arg-type]
        session_id_provider=lambda: 7,
        max_buffer_size=2,
        retry_initial_backoff_s=0.001,
        retry_max_backoff_s=0.002,
        retry_total_timeout_s=5.0,
    )
    await w.submit(_make_epoch("rover_rtk", 0))
    await w.submit(_make_epoch("rover_rtk", 1))
    assert len(repo.calls) == 1
    assert w.dropped_db_unavailable == 0
    assert w.written_total == 2


@pytest.mark.asyncio
async def test_give_up_after_total_timeout() -> None:
    repo = _FakeRepo()
    repo.fail_times = 10_000
    repo.fail_exception = asyncpg.InterfaceError("connection lost")
    w = EpochBatchWriter(
        repo,  # type: ignore[arg-type]
        session_id_provider=lambda: 7,
        max_buffer_size=2,
        retry_initial_backoff_s=0.001,
        retry_max_backoff_s=0.002,
        retry_total_timeout_s=0.01,
    )
    await w.submit(_make_epoch("rover_rtk", 0))
    await w.submit(_make_epoch("rover_rtk", 1))
    assert repo.calls == []
    assert w.dropped_db_unavailable == 2
    assert w.written_total == 0


@pytest.mark.asyncio
async def test_background_flusher_periodically_flushes() -> None:
    repo = _FakeRepo()
    w = EpochBatchWriter(
        repo,  # type: ignore[arg-type]
        session_id_provider=lambda: 7,
        flush_interval_s=0.05,
        max_buffer_size=1000,
    )
    task = asyncio.create_task(w.run_background_flusher())
    try:
        await w.submit(_make_epoch("rover_rtk", 0))
        await w.submit(_make_epoch("rover_rtk", 1))
        await asyncio.sleep(0.15)
        assert len(repo.calls) >= 1
        total_written = sum(len(c[1]) for c in repo.calls)
        assert total_written == 2
    finally:
        w.stop()
        await asyncio.wait_for(task, timeout=1.0)


@pytest.mark.asyncio
async def test_invalid_construction_args_raise() -> None:
    repo = _FakeRepo()
    with pytest.raises(ValueError):
        EpochBatchWriter(
            repo, lambda: 1, flush_interval_s=0  # type: ignore[arg-type]
        )
    with pytest.raises(ValueError):
        EpochBatchWriter(
            repo, lambda: 1, max_buffer_size=0  # type: ignore[arg-type]
        )
    with pytest.raises(ValueError):
        EpochBatchWriter(
            repo,  # type: ignore[arg-type]
            lambda: 1,
            retry_initial_backoff_s=10.0,
            retry_max_backoff_s=1.0,
        )
