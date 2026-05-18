"""Тесты репозитория аудита RTCM-сообщений."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import asyncpg
import pytest

from ntrip_accuracy_monitor.persistence.rtcm_repository import (
    RtcmMessageRecord,
    RtcmRepository,
)

_T0: datetime = datetime(2026, 5, 13, 12, 0, 0, tzinfo=UTC)


def _make_record(
    *,
    offset_s: int = 0,
    msg_type: int = 1004,
    reference_station_id: int | None = 1234,
    satellite_id: int | None = None,
    byte_length: int = 180,
) -> RtcmMessageRecord:
    return RtcmMessageRecord(
        received_at=_T0 + timedelta(seconds=offset_s),
        msg_type=msg_type,
        reference_station_id=reference_station_id,
        satellite_id=satellite_id,
        byte_length=byte_length,
    )


@pytest.mark.asyncio
async def test_insert_one_persists(
    db_conn: asyncpg.Connection,
    sample_session_id: int,
) -> None:
    repo = RtcmRepository(db_conn)
    await repo.insert_one(sample_session_id, _make_record())

    counts = await repo.count_by_msg_type(sample_session_id)
    assert counts == {1004: 1}


@pytest.mark.asyncio
async def test_insert_batch_persists_all(
    db_conn: asyncpg.Connection,
    sample_session_id: int,
) -> None:
    repo = RtcmRepository(db_conn)
    records = [
        _make_record(offset_s=i, msg_type=1004 if i % 2 == 0 else 1019)
        for i in range(10)
    ]
    await repo.insert_batch(sample_session_id, records)

    counts = await repo.count_by_msg_type(sample_session_id)
    assert counts == {1004: 5, 1019: 5}


@pytest.mark.asyncio
async def test_insert_batch_empty_is_noop(
    db_conn: asyncpg.Connection,
    sample_session_id: int,
) -> None:
    repo = RtcmRepository(db_conn)
    await repo.insert_batch(sample_session_id, [])
    counts = await repo.count_by_msg_type(sample_session_id)
    assert counts == {}


@pytest.mark.asyncio
async def test_nullable_fields_round_trip(
    db_conn: asyncpg.Connection,
    sample_session_id: int,
) -> None:
    """Проверка, что NULL в reference_station_id и satellite_id сохраняется."""
    repo = RtcmRepository(db_conn)
    record = _make_record(
        msg_type=1004,
        reference_station_id=None,
        satellite_id=None,
    )
    await repo.insert_one(sample_session_id, record)

    # Прямая проверка через SQL, что значения NULL действительно лежат
    # в базе (count_by_msg_type не различает NULL/не-NULL).
    row = await db_conn.fetchrow(
        "SELECT reference_station_id, satellite_id "
        "FROM rtcm_messages WHERE session_id = $1",
        sample_session_id,
    )
    assert row is not None
    assert row["reference_station_id"] is None
    assert row["satellite_id"] is None


@pytest.mark.asyncio
async def test_count_by_msg_type_sorted_keys_by_type(
    db_conn: asyncpg.Connection,
    sample_session_id: int,
) -> None:
    """Не упорядоченность словаря, а наличие всех типов с правильными счётами."""
    repo = RtcmRepository(db_conn)
    counts_to_insert = {1004: 5, 1006: 1, 1012: 5, 1019: 3, 1033: 1}
    offset = 0
    for msg_type, count in counts_to_insert.items():
        for _ in range(count):
            await repo.insert_one(
                sample_session_id,
                _make_record(offset_s=offset, msg_type=msg_type),
            )
            offset += 1

    counts = await repo.count_by_msg_type(sample_session_id)
    assert counts == counts_to_insert
