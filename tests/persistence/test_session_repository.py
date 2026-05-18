"""Тесты репозитория сеансов: создание, поиск текущего, завершение."""

from __future__ import annotations

import asyncpg
import pytest

from ntrip_accuracy_monitor.persistence.session_repository import (
    SessionRepository,
)


@pytest.mark.asyncio
async def test_start_returns_positive_session_id(
    db_conn: asyncpg.Connection,
) -> None:
    repo = SessionRepository(db_conn)
    session_id = await repo.start("session A")
    assert isinstance(session_id, int)
    assert session_id > 0


@pytest.mark.asyncio
async def test_get_by_id_returns_session_with_jsonb(
    db_conn: asyncpg.Connection,
) -> None:
    repo = SessionRepository(db_conn)
    session_id = await repo.start(
        description="session with antenna",
        reference_antenna={
            "latitude_deg": 55.7558,
            "longitude_deg": 37.6173,
            "ellipsoidal_height_m": 187.5,
        },
        config_snapshot={"log_level": "INFO"},
    )

    row = await repo.get_by_id(session_id)
    assert row is not None
    assert row.session_id == session_id
    assert row.description == "session with antenna"
    assert row.ended_at is None
    assert row.reference_antenna == {
        "latitude_deg": 55.7558,
        "longitude_deg": 37.6173,
        "ellipsoidal_height_m": 187.5,
    }
    assert row.config_snapshot == {"log_level": "INFO"}


@pytest.mark.asyncio
async def test_get_by_id_returns_none_for_missing(
    db_conn: asyncpg.Connection,
) -> None:
    repo = SessionRepository(db_conn)
    assert await repo.get_by_id(999_999_999) is None


@pytest.mark.asyncio
async def test_current_returns_latest_open_session(
    db_conn: asyncpg.Connection,
) -> None:
    repo = SessionRepository(db_conn)
    await repo.start("first")
    second_id = await repo.start("second")

    assert await repo.current() == second_id


@pytest.mark.asyncio
async def test_current_returns_none_when_no_open_sessions(
    db_conn: asyncpg.Connection,
) -> None:
    repo = SessionRepository(db_conn)
    session_id = await repo.start("will close")
    await repo.end(session_id)

    assert await repo.current() is None


@pytest.mark.asyncio
async def test_end_sets_ended_at(
    db_conn: asyncpg.Connection,
) -> None:
    repo = SessionRepository(db_conn)
    session_id = await repo.start("closing")
    await repo.end(session_id)

    row = await repo.get_by_id(session_id)
    assert row is not None
    assert row.ended_at is not None
    assert row.ended_at >= row.started_at


@pytest.mark.asyncio
async def test_end_is_idempotent_for_already_closed(
    db_conn: asyncpg.Connection,
) -> None:
    repo = SessionRepository(db_conn)
    session_id = await repo.start("twice closed")
    await repo.end(session_id)
    # Повторное закрытие — без ошибки, просто 0 строк затронуто.
    await repo.end(session_id)


@pytest.mark.asyncio
async def test_end_does_not_fail_on_missing_session(
    db_conn: asyncpg.Connection,
) -> None:
    repo = SessionRepository(db_conn)
    # Несуществующий ID — без ошибки, 0 строк затронуто.
    await repo.end(999_999_999)
