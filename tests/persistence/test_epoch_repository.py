"""Тесты репозитория эпох: вставка одиночная и пакетная, выборка по
времени, агрегаты по режиму решения, защита от дубликатов."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import asyncpg
import pytest

from ntrip_accuracy_monitor.domain.epoch import Epoch
from ntrip_accuracy_monitor.domain.position import GeodeticPosition
from ntrip_accuracy_monitor.domain.solution_mode import SolutionMode
from ntrip_accuracy_monitor.persistence.epoch_repository import EpochRepository

# Базовый момент времени для всех тестов — фиксированный, без now().
_T0: datetime = datetime(2026, 5, 13, 12, 0, 0, tzinfo=UTC)


def _make_epoch(
    *,
    stream_id: str = "rover_rtk",
    offset_s: int = 0,
    solution_mode: SolutionMode = SolutionMode.RTK_FIXED,
    age_of_corrections_s: float | None = 1.5,
) -> Epoch:
    """Сконструировать тестовую Epoch с разумными значениями полей."""
    return Epoch(
        epoch_time=_T0 + timedelta(seconds=offset_s),
        stream_id=stream_id,
        position=GeodeticPosition(
            latitude_deg=55.7558,
            longitude_deg=37.6173,
            ellipsoidal_height_m=187.5,
        ),
        solution_mode=solution_mode,
        age_of_corrections_s=age_of_corrections_s,
        satellites_used=14,
        hdop=0.8,
        pdop=1.2,
        sigma_east_m=0.012,
        sigma_north_m=0.010,
        sigma_up_m=0.020,
    )


@pytest.mark.asyncio
async def test_insert_one_round_trip(
    db_conn: asyncpg.Connection,
    sample_session_id: int,
) -> None:
    repo = EpochRepository(db_conn)
    original = _make_epoch()
    await repo.insert_one(sample_session_id, original)

    fetched = await repo.query_by_time_range(
        sample_session_id,
        original.stream_id,
        _T0,
        _T0 + timedelta(seconds=1),
    )
    assert len(fetched) == 1
    e = fetched[0]
    assert e.stream_id == original.stream_id
    assert e.epoch_time == original.epoch_time
    assert e.position == original.position
    assert e.solution_mode is SolutionMode.RTK_FIXED
    assert e.age_of_corrections_s == pytest.approx(1.5, rel=1e-6)
    assert e.satellites_used == 14
    assert e.hdop == pytest.approx(0.8, rel=1e-6)
    assert e.pdop == pytest.approx(1.2, rel=1e-6)
    assert e.sigma_east_m == pytest.approx(0.012, rel=1e-5)
    assert e.sigma_north_m == pytest.approx(0.010, rel=1e-5)
    assert e.sigma_up_m == pytest.approx(0.020, rel=1e-5)


@pytest.mark.asyncio
async def test_insert_batch_persists_all_records(
    db_conn: asyncpg.Connection,
    sample_session_id: int,
) -> None:
    repo = EpochRepository(db_conn)
    batch = [_make_epoch(offset_s=i) for i in range(10)]
    await repo.insert_batch(sample_session_id, batch)

    fetched = await repo.query_by_time_range(
        sample_session_id,
        "rover_rtk",
        _T0,
        _T0 + timedelta(seconds=10),
    )
    assert len(fetched) == 10


@pytest.mark.asyncio
async def test_insert_batch_empty_is_noop(
    db_conn: asyncpg.Connection,
    sample_session_id: int,
) -> None:
    repo = EpochRepository(db_conn)
    await repo.insert_batch(sample_session_id, [])
    fetched = await repo.query_by_time_range(
        sample_session_id, "rover_rtk", _T0, _T0 + timedelta(hours=1),
    )
    assert fetched == []


@pytest.mark.asyncio
async def test_duplicate_epoch_raises_unique_violation(
    db_conn: asyncpg.Connection,
    sample_session_id: int,
) -> None:
    repo = EpochRepository(db_conn)
    epoch = _make_epoch(offset_s=0)
    await repo.insert_one(sample_session_id, epoch)

    with pytest.raises(asyncpg.UniqueViolationError):
        await repo.insert_one(sample_session_id, epoch)


@pytest.mark.asyncio
async def test_query_returns_epochs_sorted_by_time(
    db_conn: asyncpg.Connection,
    sample_session_id: int,
) -> None:
    repo = EpochRepository(db_conn)
    # Намеренно вставляем в обратном порядке.
    for offset in (4, 0, 2, 1, 3):
        await repo.insert_one(sample_session_id, _make_epoch(offset_s=offset))

    fetched = await repo.query_by_time_range(
        sample_session_id, "rover_rtk", _T0, _T0 + timedelta(seconds=5),
    )
    times = [e.epoch_time for e in fetched]
    assert times == sorted(times)


@pytest.mark.asyncio
async def test_query_respects_half_open_interval(
    db_conn: asyncpg.Connection,
    sample_session_id: int,
) -> None:
    repo = EpochRepository(db_conn)
    for i in range(5):
        await repo.insert_one(sample_session_id, _make_epoch(offset_s=i))

    # Запрашиваем [_T0+1, _T0+4): ожидаем секунды 1, 2, 3.
    fetched = await repo.query_by_time_range(
        sample_session_id,
        "rover_rtk",
        _T0 + timedelta(seconds=1),
        _T0 + timedelta(seconds=4),
    )
    assert [e.epoch_time for e in fetched] == [
        _T0 + timedelta(seconds=1),
        _T0 + timedelta(seconds=2),
        _T0 + timedelta(seconds=3),
    ]


@pytest.mark.asyncio
async def test_query_filters_by_stream_id(
    db_conn: asyncpg.Connection,
    sample_session_id: int,
) -> None:
    repo = EpochRepository(db_conn)
    await repo.insert_one(
        sample_session_id, _make_epoch(stream_id="rover_rtk", offset_s=0),
    )
    await repo.insert_one(
        sample_session_id, _make_epoch(stream_id="rover_spp", offset_s=0),
    )

    rtk = await repo.query_by_time_range(
        sample_session_id, "rover_rtk", _T0, _T0 + timedelta(seconds=1),
    )
    spp = await repo.query_by_time_range(
        sample_session_id, "rover_spp", _T0, _T0 + timedelta(seconds=1),
    )
    assert len(rtk) == 1
    assert len(spp) == 1
    assert rtk[0].stream_id == "rover_rtk"
    assert spp[0].stream_id == "rover_spp"


@pytest.mark.asyncio
async def test_count_by_solution_mode_groups_correctly(
    db_conn: asyncpg.Connection,
    sample_session_id: int,
) -> None:
    repo = EpochRepository(db_conn)
    # 3 RTK_FIXED, 2 RTK_FLOAT, 1 SPP.
    modes_for_offsets = [
        (0, SolutionMode.RTK_FIXED),
        (1, SolutionMode.RTK_FIXED),
        (2, SolutionMode.RTK_FIXED),
        (3, SolutionMode.RTK_FLOAT),
        (4, SolutionMode.RTK_FLOAT),
        (5, SolutionMode.SPP),
    ]
    for offset, mode in modes_for_offsets:
        await repo.insert_one(
            sample_session_id,
            _make_epoch(
                offset_s=offset,
                solution_mode=mode,
                # SPP не имеет поправок:
                age_of_corrections_s=None if mode is SolutionMode.SPP else 1.5,
            ),
        )

    counts = await repo.count_by_solution_mode(sample_session_id)
    assert counts == {
        SolutionMode.RTK_FIXED: 3,
        SolutionMode.RTK_FLOAT: 2,
        SolutionMode.SPP: 1,
    }
