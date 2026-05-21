"""Общие фикстуры для всех тестов проекта.

Локальные определения в подкаталогах (например, tests/persistence/conftest.py
с pool на откате транзакции) переопределяют эти — стандартное поведение pytest.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator

import asyncpg
import pytest
import pytest_asyncio

from ntrip_accuracy_monitor.application.config import PostgresConfig
from ntrip_accuracy_monitor.persistence.pool import close_pool, create_pool


@pytest.fixture
def pg_config() -> PostgresConfig:
    """Параметры подключения к dev-БД для интеграционных тестов.

    Дефолты соответствуют [postgres] в config.toml dev-окружения.
    Все переопределяемы env-переменными.
    PG_PASSWORD — обязательная; если не задана — тест пропускается.
    """
    pg_password = os.environ.get("PG_PASSWORD")
    if not pg_password:
        pytest.skip("PG_PASSWORD не задан — интеграционный тест пропущен")
    return PostgresConfig(
        host=os.environ.get("PG_HOST", "127.0.0.1"),
        port=int(os.environ.get("PG_PORT", "5432")),
        database=os.environ.get("PG_DATABASE", "ntrip_accuracy_monitor"),
        user=os.environ.get("PG_USER", "ntrip_app"),
        password=pg_password,  # type: ignore[arg-type]  # SecretStr принимает str
    )


@pytest_asyncio.fixture
async def pool(pg_config: PostgresConfig) -> AsyncIterator[asyncpg.Pool]:
    """Набор соединений к dev-БД, живёт ровно в рамках одного теста.

    Function scope (а не session): интеграционные тесты пишут в БД и
    потом проверяют — чище держать «один тест — один набор соединений».
    Если в будущем тесты станут тяжёлыми, оптимизация — отдельной задачей.

    tests/persistence/conftest.py может переопределить эту фикстуру
    своим вариантом с откатом транзакций; pytest возьмёт ближайшую
    к тесту версию, тут конфликта нет.
    """
    pool_inst = await create_pool(pg_config)
    try:
        yield pool_inst
    finally:
        await close_pool(pool_inst)
