"""Репозиторий эпох ровера.

Основная таблица результатов. Запись ведётся пакетами через ``COPY``
для производительности — 1 Гц × 3 приёмника даёт 180 записей за минуту.

Domain ↔ DB:
    Epoch.stream_id          ⇄ epochs.stream_id
    Epoch.position.*         ⇄ epochs.latitude_deg/longitude_deg/ellipsoidal_height_m
    Epoch.solution_mode      ⇄ epochs.solution_mode (IntEnum value)
    Epoch.sigma_*_m          ⇄ epochs.sigma_*_m
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Final

from ntrip_accuracy_monitor.domain.epoch import Epoch
from ntrip_accuracy_monitor.domain.position import GeodeticPosition
from ntrip_accuracy_monitor.domain.solution_mode import SolutionMode
from ntrip_accuracy_monitor.persistence._executor import (
    Executor,
    acquire_connection,
)

_COLUMNS: Final = (
    "session_id",
    "stream_id",
    "epoch_time",
    "latitude_deg",
    "longitude_deg",
    "ellipsoidal_height_m",
    "solution_mode",
    "age_of_corrections_s",
    "satellites_used",
    "hdop",
    "pdop",
    "sigma_east_m",
    "sigma_north_m",
    "sigma_up_m",
)

_INSERT_ONE_SQL: Final = f"""\
INSERT INTO epochs ({', '.join(_COLUMNS)})
VALUES ({', '.join(f'${i + 1}' for i in range(len(_COLUMNS)))})
"""

_QUERY_BY_TIME_RANGE_SQL: Final = """\
SELECT stream_id, epoch_time,
       latitude_deg, longitude_deg, ellipsoidal_height_m,
       solution_mode,
       age_of_corrections_s, satellites_used,
       hdop, pdop,
       sigma_east_m, sigma_north_m, sigma_up_m
FROM epochs
WHERE session_id = $1
  AND stream_id  = $2
  AND epoch_time >= $3
  AND epoch_time <  $4
ORDER BY epoch_time
"""

_COUNT_BY_MODE_SQL: Final = """\
SELECT solution_mode, COUNT(*) AS cnt
FROM epochs
WHERE session_id = $1
GROUP BY solution_mode
"""

_FETCH_FOR_SESSION_STREAM_SQL: Final = """\
SELECT stream_id, epoch_time,
       latitude_deg, longitude_deg, ellipsoidal_height_m,
       solution_mode,
       age_of_corrections_s, satellites_used,
       hdop, pdop,
       sigma_east_m, sigma_north_m, sigma_up_m
FROM epochs
WHERE session_id = $1
  AND stream_id  = $2
ORDER BY epoch_time
"""


class EpochRepository:
    """Запись и чтение эпох ровера."""

    def __init__(self, executor: Executor) -> None:
        self._executor = executor

    async def insert_one(self, session_id: int, epoch: Epoch) -> None:
        """Вставить одну эпоху. Для тестов и отладки, не для горячего пути."""
        async with acquire_connection(self._executor) as conn:
            await conn.execute(
                _INSERT_ONE_SQL,
                *self._epoch_to_row_values(session_id, epoch),
            )

    async def insert_batch(
        self,
        session_id: int,
        epochs: Sequence[Epoch],
    ) -> None:
        """Пакетная вставка эпох через PostgreSQL COPY.

        Для пустой последовательности — ничего не делает.

        При попадании дубликата по ``(session_id, stream_id, epoch_time)``
        вся партия откатывается и поднимается ``asyncpg.UniqueViolationError``.
        """
        if not epochs:
            return
        records = [
            self._epoch_to_row_values(session_id, epoch)
            for epoch in epochs
        ]
        async with acquire_connection(self._executor) as conn:
            await conn.copy_records_to_table(
                "epochs",
                records=records,
                columns=_COLUMNS,
            )

    async def query_by_time_range(
        self,
        session_id: int,
        stream_id: str,
        start: datetime,
        end: datetime,
    ) -> list[Epoch]:
        """Вернуть эпохи канала в полуоткрытом интервале [start, end)."""
        async with acquire_connection(self._executor) as conn:
            rows = await conn.fetch(
                _QUERY_BY_TIME_RANGE_SQL,
                session_id,
                stream_id,
                start,
                end,
            )
        return [self._row_to_epoch(row) for row in rows]

    async def fetch_for_session_stream(
        self,
        session_id: int,
        stream_id: str,
    ) -> list[Epoch]:
        """Вернуть все эпохи указанного канала в сеансе, отсортированные по времени.

        Используется сервисом расчёта метрик. Контракт сортировки по
        epoch_time — обязательная часть API: на нём держится расчёт
        ttff_s в SolutionModeFilter.RTK_FIXED_FLOAT (см. domain/metrics.py).
        """
        async with acquire_connection(self._executor) as conn:
            rows = await conn.fetch(
                _FETCH_FOR_SESSION_STREAM_SQL,
                session_id,
                stream_id,
            )
        return [self._row_to_epoch(row) for row in rows]

    async def count_by_solution_mode(
        self,
        session_id: int,
    ) -> dict[SolutionMode, int]:
        """Количество эпох по каждому значению solution_mode в сеансе."""
        async with acquire_connection(self._executor) as conn:
            rows = await conn.fetch(_COUNT_BY_MODE_SQL, session_id)
        return {SolutionMode(row["solution_mode"]): row["cnt"] for row in rows}

    @staticmethod
    def _epoch_to_row_values(
        session_id: int,
        epoch: Epoch,
    ) -> tuple[
        int, str, datetime,
        float, float, float,
        int,
        float | None, int | None,
        float | None, float | None,
        float | None, float | None, float | None,
    ]:
        return (
            session_id,
            epoch.stream_id,
            epoch.epoch_time,
            epoch.position.latitude_deg,
            epoch.position.longitude_deg,
            epoch.position.ellipsoidal_height_m,
            int(epoch.solution_mode),
            epoch.age_of_corrections_s,
            epoch.satellites_used,
            epoch.hdop,
            epoch.pdop,
            epoch.sigma_east_m,
            epoch.sigma_north_m,
            epoch.sigma_up_m,
        )

    @staticmethod
    def _row_to_epoch(row: object) -> Epoch:
        return Epoch(
            epoch_time=row["epoch_time"],  # type: ignore[index]
            stream_id=row["stream_id"],  # type: ignore[index]
            position=GeodeticPosition(
                latitude_deg=row["latitude_deg"],  # type: ignore[index]
                longitude_deg=row["longitude_deg"],  # type: ignore[index]
                ellipsoidal_height_m=row["ellipsoidal_height_m"],  # type: ignore[index]
            ),
            solution_mode=SolutionMode(row["solution_mode"]),  # type: ignore[index]
            age_of_corrections_s=row["age_of_corrections_s"],  # type: ignore[index]
            satellites_used=row["satellites_used"],  # type: ignore[index]
            hdop=row["hdop"],  # type: ignore[index]
            pdop=row["pdop"],  # type: ignore[index]
            sigma_east_m=row["sigma_east_m"],  # type: ignore[index]
            sigma_north_m=row["sigma_north_m"],  # type: ignore[index]
            sigma_up_m=row["sigma_up_m"],  # type: ignore[index]
        )
