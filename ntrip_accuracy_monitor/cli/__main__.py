"""Точка входа в приложение ntrip-accuracy-monitor.

Запускается двумя способами:
  - python -m ntrip_accuracy_monitor
  - ntrip-accuracy-monitor             (через entry-point скрипт)

Минимально: находит config.toml, инициализирует логирование, создаёт
набор соединений к БД, запускает SessionLifecycle, ждёт его завершения.
Сигналы SIGINT/SIGTERM обрабатываются внутри SessionLifecycle.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from pathlib import Path
from typing import Final

from ntrip_accuracy_monitor.application.config import AppConfig, load_config
from ntrip_accuracy_monitor.application.service.lifecycle import (
    SessionLifecycle,
)
from ntrip_accuracy_monitor.persistence.pool import close_pool, create_pool

_CONFIG_ENV_VAR: Final[str] = "NAM_CONFIG_PATH"
_DEFAULT_CONFIG_FILENAME: Final[str] = "config.toml"

logger: Final = logging.getLogger("ntrip_accuracy_monitor")


def main() -> None:
    """Sync-точка входа. Вся работа в _async_main под asyncio.run."""
    try:
        config_path = _locate_config()
        config = load_config(config_path)
    except (FileNotFoundError, ValueError) as exc:
        # Ошибки конфигурации видны оператору без traceback'а — логирование
        # ещё не настроено, поэтому пишем напрямую в stderr.
        print(f"Configuration error: {exc}", file=sys.stderr)
        sys.exit(2)

    _setup_logging(config.log_level)
    logger.info("Loaded configuration from %s", config_path)

    try:
        asyncio.run(_async_main(config))
    except KeyboardInterrupt:
        # Финальная страховка: SessionLifecycle должен ловить SIGINT сам,
        # но если ctrl-C прилетел до установки обработчика — выход без traceback.
        logger.info("Interrupted before signal handlers were installed")
        sys.exit(130)


async def _async_main(config: AppConfig) -> None:
    """Главный async-цикл: пул соединений, SessionLifecycle, корректное закрытие."""
    pool = await create_pool(config.postgres)
    try:
        lifecycle = SessionLifecycle(config=config, pool=pool)
        await lifecycle.run()
    finally:
        await close_pool(pool)


def _locate_config() -> Path:
    """Найти файл конфигурации.

    Приоритет:
      1. Переменная окружения NAM_CONFIG_PATH.
      2. ./config.toml в текущей рабочей директории.

    Raises:
        FileNotFoundError: если ни один из путей не указывает на файл.
    """
    env_path = os.environ.get(_CONFIG_ENV_VAR)
    if env_path:
        candidate = Path(env_path)
        if not candidate.is_file():
            raise FileNotFoundError(
                f"{_CONFIG_ENV_VAR}={env_path!r} does not point to a file"
            )
        return candidate

    candidate = Path.cwd() / _DEFAULT_CONFIG_FILENAME
    if not candidate.is_file():
        raise FileNotFoundError(
            f"Configuration file not found: {candidate}. "
            f"Set {_CONFIG_ENV_VAR} to an explicit path, or run from "
            f"a directory containing {_DEFAULT_CONFIG_FILENAME}."
        )
    return candidate


def _setup_logging(level: str) -> None:
    """Базовая настройка logging. Полная конфигурация — чат №12."""
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )


if __name__ == "__main__":
    main()
