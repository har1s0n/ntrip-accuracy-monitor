"""Тесты репозитория сеансов: создание, поиск текущего, завершение."""

from __future__ import annotations

import asyncpg
import pytest

from ntrip_accuracy_monitor.persistence.session_repository import (
    SessionRepository,
    TerminationReason,
)


# ---------------------------------------------------------------------------
# Создание сеанса
# ---------------------------------------------------------------------------
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
    assert row.termination_reason is None  # сеанс открыт
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


# ---------------------------------------------------------------------------
# Поиск текущего сеанса
# ---------------------------------------------------------------------------
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
    await repo.end(session_id, "normal")

    assert await repo.current() is None


# ---------------------------------------------------------------------------
# Завершение сеанса: ended_at и termination_reason
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_end_sets_ended_at_and_reason(
    db_conn: asyncpg.Connection,
) -> None:
    repo = SessionRepository(db_conn)
    session_id = await repo.start("closing")
    await repo.end(session_id, "normal")

    row = await repo.get_by_id(session_id)
    assert row is not None
    assert row.ended_at is not None
    assert row.ended_at >= row.started_at
    assert row.termination_reason == "normal"


@pytest.mark.parametrize("reason", ["normal", "signal", "error"])
@pytest.mark.asyncio
async def test_end_records_each_termination_reason(
    db_conn: asyncpg.Connection,
    reason: TerminationReason,
) -> None:
    repo = SessionRepository(db_conn)
    session_id = await repo.start(f"end with reason={reason}")
    await repo.end(session_id, reason)

    row = await repo.get_by_id(session_id)
    assert row is not None
    assert row.termination_reason == reason


@pytest.mark.asyncio
async def test_end_is_idempotent_keeps_first_reason(
    db_conn: asyncpg.Connection,
) -> None:
    """Повторный end не падает и не перезаписывает уже зафиксированную причину.

    Защита через WHERE ended_at IS NULL: вторая попытка затронет 0 строк,
    первая причина останется в БД.
    """
    repo = SessionRepository(db_conn)
    session_id = await repo.start("twice closed")
    await repo.end(session_id, "normal")
    # Повторное закрытие с другой причиной — без ошибки, без перезаписи.
    await repo.end(session_id, "error")

    row = await repo.get_by_id(session_id)
    assert row is not None
    assert row.termination_reason == "normal"


@pytest.mark.asyncio
async def test_end_does_not_fail_on_missing_session(
    db_conn: asyncpg.Connection,
) -> None:
    repo = SessionRepository(db_conn)
    # Несуществующий ID — без ошибки, 0 строк затронуто.
    await repo.end(999_999_999, "normal")


# ---------------------------------------------------------------------------
# CHECK-ограничения в БД: значение причины и консистентность с ended_at.
# Каждый тест работает на собственном сеансе в собственной транзакции —
# нарушение CHECK переводит транзакцию в aborted, откат в фикстуре чинит.
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_check_rejects_invalid_termination_reason_value(
    db_conn: asyncpg.Connection,
) -> None:
    """Ограничение sessions_termination_reason_values: разрешены только
    'normal', 'signal', 'error'. Любое другое значение должно падать."""
    repo = SessionRepository(db_conn)
    session_id = await repo.start("invalid reason check")

    with pytest.raises(asyncpg.exceptions.CheckViolationError):
        await db_conn.execute(
            "UPDATE sessions "
            "SET ended_at = now(), termination_reason = $1 "
            "WHERE session_id = $2",
            "bogus",
            session_id,
        )


@pytest.mark.asyncio
async def test_check_rejects_ended_at_without_reason(
    db_conn: asyncpg.Connection,
) -> None:
    """Ограничение sessions_termination_consistency: нельзя проставить
    ended_at без termination_reason."""
    repo = SessionRepository(db_conn)
    session_id = await repo.start("ended_at without reason")

    with pytest.raises(asyncpg.exceptions.CheckViolationError):
        await db_conn.execute(
            "UPDATE sessions SET ended_at = now() WHERE session_id = $1",
            session_id,
        )


@pytest.mark.asyncio
async def test_check_rejects_reason_without_ended_at(
    db_conn: asyncpg.Connection,
) -> None:
    """Ограничение sessions_termination_consistency: нельзя проставить
    termination_reason без ended_at."""
    repo = SessionRepository(db_conn)
    session_id = await repo.start("reason without ended_at")

    with pytest.raises(asyncpg.exceptions.CheckViolationError):
        await db_conn.execute(
            "UPDATE sessions SET termination_reason = 'normal' "
            "WHERE session_id = $1",
            session_id,
        )


@pytest.mark.asyncio
async def test_check_allows_clearing_both_columns_back_to_open(
    db_conn: asyncpg.Connection,
) -> None:
    """Ограничение sessions_termination_consistency симметрично: оба
    NULL → допустимо (хотя репозиторий такого API не предоставляет —
    проверяем, что констрейнт сам по себе непротиворечив).
    """
    repo = SessionRepository(db_conn)
    session_id = await repo.start("close then reopen")
    await repo.end(session_id, "normal")

    # Прямой SQL: возвращаем обе колонки в NULL — должно пройти.
    await db_conn.execute(
        "UPDATE sessions SET ended_at = NULL, termination_reason = NULL "
        "WHERE session_id = $1",
        session_id,
    )
    row = await repo.get_by_id(session_id)
    assert row is not None
    assert row.ended_at is None
    assert row.termination_reason is None
