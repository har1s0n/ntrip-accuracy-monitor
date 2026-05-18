"""Репозиторий аудита RTCM-сообщений от базы.

Хранятся только метаданные кадра — сами байты не сохраняем.
Запись ведётся для всех типов сообщений, которые приходят
в потоке: наблюдения (1002/1004/1010/1012), координаты базы (1006),
эфемериды (1019/1020), дескриптор станции (1033) и так далее.

В горячем пути писать предполагается пакетами через ``insert_batch``.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Final

from ntrip_accuracy_monitor.persistence._executor import (
    Executor,
    acquire_connection,
)


@dataclass(frozen=True, slots=True)
class RtcmMessageRecord:
    """Метаданные одного RTCM-кадра для записи в аудит."""

    received_at: datetime
    msg_type: int
    reference_station_id: int | None
    satellite_id: int | None
    byte_length: int


_COLUMNS: Final = (
    "session_id",
    "received_at",
    "msg_type",
    "reference_station_id",
    "satellite_id",
    "byte_length",
)

_INSERT_ONE_SQL: Final = f"""\
INSERT INTO rtcm_messages ({', '.join(_COLUMNS)})
VALUES ({', '.join(f'${i + 1}' for i in range(len(_COLUMNS)))})
"""

_COUNT_BY_TYPE_SQL: Final = """\
SELECT msg_type, COUNT(*) AS cnt
FROM rtcm_messages
WHERE session_id = $1
GROUP BY msg_type
ORDER BY msg_type
"""


class RtcmRepository:
    """Запись метаданных RTCM-сообщений и простые агрегаты."""

    def __init__(self, executor: Executor) -> None:
        self._executor = executor

    async def insert_one(
        self,
        session_id: int,
        record: RtcmMessageRecord,
    ) -> None:
        """Вставить одну запись аудита. Для тестов и отладки."""
        async with acquire_connection(self._executor) as conn:
            await conn.execute(
                _INSERT_ONE_SQL,
                session_id,
                record.received_at,
                record.msg_type,
                record.reference_station_id,
                record.satellite_id,
                record.byte_length,
            )

    async def insert_batch(
        self,
        session_id: int,
        records: Sequence[RtcmMessageRecord],
    ) -> None:
        """Пакетная вставка записей аудита через PostgreSQL COPY.

        Для пустой последовательности — ничего не делает.
        """
        if not records:
            return
        rows = [
            (
                session_id,
                record.received_at,
                record.msg_type,
                record.reference_station_id,
                record.satellite_id,
                record.byte_length,
            )
            for record in records
        ]
        async with acquire_connection(self._executor) as conn:
            await conn.copy_records_to_table(
                "rtcm_messages",
                records=rows,
                columns=_COLUMNS,
            )

    async def count_by_msg_type(self, session_id: int) -> dict[int, int]:
        """Распределение количества сообщений по типам в сеансе.

        Используется для аудита: «сколько 1004 пришло за сеанс A»,
        «приходили ли вообще 1019», и так далее.
        """
        async with acquire_connection(self._executor) as conn:
            rows = await conn.fetch(_COUNT_BY_TYPE_SQL, session_id)
        return {row["msg_type"]: row["cnt"] for row in rows}
