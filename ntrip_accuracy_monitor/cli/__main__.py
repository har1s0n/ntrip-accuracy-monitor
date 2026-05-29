"""Точка входа в приложение ntrip-accuracy-monitor.

Запускается двумя способами:
  - python -m ntrip_accuracy_monitor
  - ntrip-accuracy-monitor             (через entry-point скрипт)

Диспетчер подкоманд (argparse). Глобальные опции идут ДО подкоманды:
  ntrip-accuracy-monitor [--config PATH] [--log-level LEVEL] <команда>

Команды:
  run   preflight (БД + миграции) → полный пайплайн с приёмников.
        Сигналы SIGINT/SIGTERM обрабатываются внутри SessionLifecycle.

Коды возврата:
  0    штатное завершение
  2    ошибка конфигурации / неизвестная команда
  3    preflight не пройден (БД недоступна или есть непримененные миграции)
  130  Ctrl-C до установки обработчиков сигналов
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from pathlib import Path
from typing import Final

import asyncpg

from ntrip_accuracy_monitor.application.config import AppConfig, load_config
from ntrip_accuracy_monitor.application.service.lifecycle import (
    SessionLifecycle,
)
from ntrip_accuracy_monitor.persistence.migrator import pending_migrations
from ntrip_accuracy_monitor.persistence.pool import close_pool, create_pool

_CONFIG_ENV_VAR: Final[str] = "NAM_CONFIG_PATH"
_DEFAULT_CONFIG_FILENAME: Final[str] = "config.toml"

logger: Final = logging.getLogger("ntrip_accuracy_monitor")


class _PreflightError(Exception):
    """Преполётная проверка не пройдена — fail-fast до старта пайплайна."""


def main() -> None:
    """Sync-точка входа: разбор аргументов, конфиг, логирование, диспетчеризация."""
    args = _parse_args()

    try:
        config_path = _resolve_config_path(args.config)
        config = load_config(config_path)
    except (FileNotFoundError, ValueError) as exc:
        # Логирование ещё не настроено — пишем напрямую в stderr, без traceback.
        print(f"Configuration error: {exc}", file=sys.stderr)
        sys.exit(2)

    _setup_logging(args.log_level or config.log_level)
    logger.info("Loaded configuration from %s", config_path)

    match args.command:
        case "run":
            _run_command(config)
        case _:  # argparse required=True не должен сюда пускать
            print(f"Unknown command: {args.command!r}", file=sys.stderr)
            sys.exit(2)


def _run_command(config: AppConfig) -> None:
    """Подкоманда run: preflight + полный пайплайн под asyncio.run."""
    try:
        asyncio.run(_async_main(config))
    except _PreflightError as exc:
        logger.error("Preflight не пройден: %s", exc)
        sys.exit(3)
    except KeyboardInterrupt:
        # Страховка: SessionLifecycle ловит SIGINT сам, но если Ctrl-C
        # прилетел до установки обработчика — выходим без traceback.
        logger.info("Interrupted before signal handlers were installed")
        sys.exit(130)


async def _async_main(config: AppConfig) -> None:
    """Пул соединений → preflight миграций → SessionLifecycle → закрытие."""
    try:
        pool = await create_pool(config.postgres)
    except (OSError, asyncpg.PostgresError) as exc:
        raise _PreflightError(
            f"PostgreSQL недоступен "
            f"({config.postgres.host}:{config.postgres.port}/"
            f"{config.postgres.database}): {exc}"
        ) from exc

    try:
        await _preflight_migrations(pool)
        lifecycle = SessionLifecycle(config=config, pool=pool)
        await lifecycle.run()
    finally:
        await close_pool(pool)


async def _preflight_migrations(pool: asyncpg.Pool) -> None:
    """Fail-fast, если есть непримененные миграции.

    Без актуальной схемы писать epochs/rtcm_messages/метрики некуда —
    лучше упасть с понятной подсказкой, чем стартовать пайплайн в БД
    без нужных таблиц. Авто-наката НЕ делаем намеренно: миграции —
    явная операция (python -m ...persistence.migrator).
    """
    try:
        pending = await pending_migrations(pool)
    except (FileNotFoundError, ValueError, RuntimeError, asyncpg.PostgresError) as exc:
        raise _PreflightError(f"проверка миграций не удалась: {exc}") from exc

    if pending:
        raise _PreflightError(
            "не применены миграции: "
            + ", ".join(pending)
            + ". Примените: python -m ntrip_accuracy_monitor.persistence.migrator "
              "--config <config.toml>"
        )
    logger.info("Preflight: схема БД актуальна")


def _parse_args() -> argparse.Namespace:
    """Разбор аргументов. Глобальные опции — ДО подкоманды.

    --config/--log-level определены только на верхнем парсере (не на
    подкоманде), чтобы избежать известного бага argparse с перезаписью
    значений дефолтами субпарсера (bpo-9351).
    """
    parser = argparse.ArgumentParser(
        prog="ntrip-accuracy-monitor",
        description="Мониторинг точности дифференциальных поправок NTRIP/GNSS.",
    )
    parser.add_argument(
        "--config",
        type=str,
        default=None,
        metavar="PATH",
        help=(
            "Путь к config.toml. Приоритет над "
            f"{_CONFIG_ENV_VAR} и ./{_DEFAULT_CONFIG_FILENAME}."
        ),
    )
    parser.add_argument(
        "--log-level",
        default=None,
        choices=("DEBUG", "INFO", "WARNING", "ERROR"),
        help="Переопределить log_level из конфига.",
    )

    sub = parser.add_subparsers(dest="command", required=True, metavar="команда")
    sub.add_parser("run", help="Поднять полный пайплайн с приёмников.")
    return parser.parse_args()


def _resolve_config_path(cli_config: str | None) -> Path:
    """--config (приоритет) → NAM_CONFIG_PATH → ./config.toml."""
    if cli_config:
        candidate = Path(cli_config)
        if not candidate.is_file():
            raise FileNotFoundError(
                f"--config={cli_config!r} does not point to a file"
            )
        return candidate
    return _locate_config()


def _locate_config() -> Path:
    """Найти файл конфигурации: NAM_CONFIG_PATH, затем ./config.toml.

    Raises:
        FileNotFoundError: если ни один путь не указывает на файл.
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
    """Базовая настройка logging в stdout (plain-формат)."""
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )


if __name__ == "__main__":
    main()
