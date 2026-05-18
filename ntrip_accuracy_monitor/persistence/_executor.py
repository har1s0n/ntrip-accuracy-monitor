"""Общий тип SQL-исполнителя и помощник захвата соединения.

Репозитории принимают либо ``asyncpg.Pool``, либо ``asyncpg.Connection``,
чтобы поддерживать два паттерна работы:

1. Рабочий код: репозиторий получает Pool, на каждую операцию сам берёт
   и возвращает соединение через ``pool.acquire()``.
2. Тесты с откатом транзакций: репозиторий получает Connection с уже
   открытой транзакцией; пишет/читает в эту транзакцию, откат происходит
   автоматически по окончании теста.

Через ``acquire_connection`` оба случая обрабатываются единым кодом.
Префикс ``_`` в имени файла подчёркивает: модуль внутренний для пакета
persistence, не публичный API.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import asyncpg

type Executor = asyncpg.Pool | asyncpg.Connection


@asynccontextmanager
async def acquire_connection(
    executor: Executor,
) -> AsyncIterator[asyncpg.Connection]:
    """Получить рабочее соединение из исполнителя.

    Для ``Pool`` — берёт соединение из набора, возвращает по выходу из
    контекста. Для ``Connection`` — отдаёт его как есть, без захвата
    и возврата.
    """
    if isinstance(executor, asyncpg.Pool):
        async with executor.acquire() as conn:
            yield conn
    else:
        yield executor
