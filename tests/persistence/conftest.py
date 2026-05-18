"""Фикстуры для тестов репозиториев persistence.

Тесты работают с реальной локальной базой ``ntrip_accuracy_monitor``,
к которой подключается приложение. Изоляция между тестами — через
откат транзакций: каждая фикстура ``db_conn`` открывает транзакцию
на соединении, тест в ней пишет/читает, по окончанию транзакция
откатывается. Данные не остаются в базе после прогона тестов.

Это подразумевает, что миграции к базе уже применены: тесты не
создают и не применяют миграции, они только проверяют поведение
репозиториев на готовой схеме.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import asyncpg
import pytest
import pytest_asyncio

from ntrip_accuracy_monitor.application.config import AppConfig, load_config
from ntrip_accuracy_monitor.persistence.pool import close_pool, create_pool
from ntrip_accuracy_monitor.persistence.session_repository import (
    SessionRepository,
)


@pytest.fixture(scope="session")
def app_config() -> AppConfig:
    """Конфигурация приложения, загружается один раз на pytest-сессию."""
    return load_config(Path("config.toml"))


@pytest_asyncio.fixture
async def db_pool(app_config: AppConfig) -> AsyncIterator[asyncpg.Pool]:
    """Набор соединений к рабочей базе на время одного теста.

    Function-scope намеренно: даёт независимость тестов и не требует
    настройки session-scope event loop в pytest-asyncio. Стоимость
    создания/закрытия пула — порядка десятков миллисекунд.
    """
    pool = await create_pool(app_config.postgres)
    try:
        yield pool
    finally:
        await close_pool(pool)


@pytest_asyncio.fixture
async def db_conn(
    db_pool: asyncpg.Pool,
) -> AsyncIterator[asyncpg.Connection]:
    """Соединение с открытой транзакцией. Откат гарантирован по окончанию.

    Передаётся в конструкторы репозиториев как Executor. Всё, что
    репозиторий запишет в этой транзакции, исчезнет по выходу из теста.
    """
    async with db_pool.acquire() as conn:
        tr = conn.transaction()
        await tr.start()
        try:
            yield conn
        finally:
            await tr.rollback()


@pytest_asyncio.fixture
async def sample_session_id(db_conn: asyncpg.Connection) -> int:
    """Готовый ID сеанса для тестов, которым нужен внешний ключ.

    Создаётся внутри транзакции теста, удаляется автоматически с откатом.
    """
    repo = SessionRepository(db_conn)
    return await repo.start("test session")
