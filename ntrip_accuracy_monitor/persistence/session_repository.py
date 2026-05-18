"""Репозиторий сеансов наблюдений.

Сеанс — это лабораторный прогон по дизайну эксперимента.
В рамках одного сеанса данные с трёх приёмников группируются вместе
для последующего расчёта метрик.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final

from ntrip_accuracy_monitor.persistence._executor import (
    Executor,
    acquire_connection,
)


@dataclass(frozen=True, slots=True)
class SessionRow:
    """Строка таблицы sessions в типизированном виде."""

    session_id: int
    started_at: datetime
    ended_at: datetime | None
    description: str
    reference_antenna: dict[str, Any] | None
    config_snapshot: dict[str, Any] | None


_INSERT_SQL: Final = """\
INSERT INTO sessions (description, reference_antenna, config_snapshot)
VALUES ($1, $2, $3)
RETURNING session_id
"""

_END_SQL: Final = """\
UPDATE sessions
SET ended_at = now()
WHERE session_id = $1
  AND ended_at IS NULL
"""

_CURRENT_SQL: Final = """\
SELECT session_id
FROM sessions
WHERE ended_at IS NULL
ORDER BY started_at DESC, session_id DESC
LIMIT 1
"""

_GET_BY_ID_SQL: Final = """\
SELECT session_id, started_at, ended_at, description,
       reference_antenna, config_snapshot
FROM sessions
WHERE session_id = $1
"""


class SessionRepository:
    """Создание, завершение и поиск сеансов наблюдений."""

    def __init__(self, executor: Executor) -> None:
        self._executor = executor

    async def start(
        self,
        description: str,
        reference_antenna: dict[str, Any] | None = None,
        config_snapshot: dict[str, Any] | None = None,
    ) -> int:
        """Открыть новый сеанс наблюдений, вернуть присвоенный session_id."""
        async with acquire_connection(self._executor) as conn:
            row = await conn.fetchrow(
                _INSERT_SQL,
                description,
                reference_antenna,
                config_snapshot,
            )
        assert row is not None
        return row["session_id"]

    async def end(self, session_id: int) -> None:
        """Завершить сеанс: проставить ``ended_at = now()``."""
        async with acquire_connection(self._executor) as conn:
            await conn.execute(_END_SQL, session_id)

    async def current(self) -> int | None:
        """Найти самый свежий незавершённый сеанс."""
        async with acquire_connection(self._executor) as conn:
            row = await conn.fetchrow(_CURRENT_SQL)
        return row["session_id"] if row is not None else None

    async def get_by_id(self, session_id: int) -> SessionRow | None:
        """Получить полную запись сеанса по идентификатору."""
        async with acquire_connection(self._executor) as conn:
            row = await conn.fetchrow(_GET_BY_ID_SQL, session_id)
        if row is None:
            return None
        return SessionRow(
            session_id=row["session_id"],
            started_at=row["started_at"],
            ended_at=row["ended_at"],
            description=row["description"],
            reference_antenna=row["reference_antenna"],
            config_snapshot=row["config_snapshot"],
        )
