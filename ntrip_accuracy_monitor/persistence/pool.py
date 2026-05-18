"""Создание набора соединений к PostgreSQL из конфигурации приложения.

Один набор соединений живёт на всё приложение и передаётся в репозитории
и в мигратор. Закрывается при остановке.

На каждом соединении настраиваются кодеки JSON/JSONB — без этого asyncpg
возвращает JSONB как сырую строку JSON, и каждый репозиторий вынужден
сам делать json.loads. С кодеками преобразование dict <-> JSONB
выполняется на уровне драйвера.
"""

from __future__ import annotations

import json
import logging
from typing import Final

import asyncpg

from ntrip_accuracy_monitor.application.config import PostgresConfig

_logger: Final = logging.getLogger(__name__)


async def _init_connection(conn: asyncpg.Connection) -> None:
    """Настроить кодеки JSON/JSONB на свежесозданном соединении."""
    for typename in ("json", "jsonb"):
        await conn.set_type_codec(
            typename,
            encoder=json.dumps,
            decoder=json.loads,
            schema="pg_catalog",
        )


async def create_pool(config: PostgresConfig) -> asyncpg.Pool:
    """Создать набор соединений к PostgreSQL.

    Параметры подключения — из ``PostgresConfig``. Размеры набора — из
    полей ``min_pool_size`` и ``max_pool_size``. Каждое соединение
    инициализируется кодеками JSON/JSONB через ``_init_connection``.

    Поднимает ``asyncpg.PostgresError``, если подключиться не удалось.
    """
    _logger.info(
        "Создание набора соединений PostgreSQL: %s@%s:%d/%s, размер %d..%d",
        config.user,
        config.host,
        config.port,
        config.database,
        config.min_pool_size,
        config.max_pool_size,
    )
    pool = await asyncpg.create_pool(
        host=config.host,
        port=config.port,
        user=config.user,
        password=config.password.get_secret_value(),
        database=config.database,
        min_size=config.min_pool_size,
        max_size=config.max_pool_size,
        init=_init_connection,
    )
    assert pool is not None
    return pool


async def close_pool(pool: asyncpg.Pool) -> None:
    """Закрыть набор соединений, дождавшись завершения активных операций."""
    _logger.info("Закрытие набора соединений PostgreSQL")
    await pool.close()
