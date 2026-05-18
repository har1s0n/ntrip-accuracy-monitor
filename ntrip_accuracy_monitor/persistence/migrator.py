"""Управление миграциями PostgreSQL.

Каждый файл ``V<NNN>__<description>.sql`` из каталога ``migrations/``
применяется один раз. Состояние отслеживается в служебной таблице
``schema_migrations`` внутри самой базы. При повторных запусках уже
применённые миграции пропускаются.

Каждая миграция выполняется в отдельной транзакции. При сбое конкретной
миграции работа прекращается, ранее применённые остаются в базе
в целостном состоянии.

CLI-точка входа::

    python -m ntrip_accuracy_monitor.persistence.migrator --config config.toml
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import re
from pathlib import Path
from typing import Final

import asyncpg

from ntrip_accuracy_monitor.application.config import load_config
from ntrip_accuracy_monitor.persistence.pool import close_pool, create_pool

_logger: Final = logging.getLogger(__name__)

_MIGRATIONS_DIR_DEFAULT: Final = Path(__file__).parent / "migrations"
_MIGRATION_FILENAME_RE: Final = re.compile(r"^V(\d{3})__([a-z0-9_]+)\.sql$")

_CREATE_META_TABLE_SQL: Final = """\
CREATE TABLE IF NOT EXISTS schema_migrations (
    version    TEXT PRIMARY KEY,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
)
"""


async def apply_migrations(
    pool: asyncpg.Pool,
    migrations_dir: Path | None = None,
) -> list[str]:
    """Применить недостающие миграции из каталога к базе.

    Args:
        pool: Набор соединений к целевой базе.
        migrations_dir: Каталог с файлами миграций. По умолчанию —
            каталог ``migrations/`` рядом с этим модулем.

    Returns:
        Список имён миграций, применённых на этом запуске, в порядке
        применения. Пустой список — все миграции уже были применены.

    Raises:
        FileNotFoundError: каталога с миграциями не существует.
        ValueError: имя файла не соответствует формату V<NNN>__<name>.sql.
        RuntimeError: в ``schema_migrations`` есть запись, для которой
            нет файла на диске.
        asyncpg.PostgresError: SQL-команда внутри миграции упала.
    """
    if migrations_dir is None:
        migrations_dir = _MIGRATIONS_DIR_DEFAULT

    if not migrations_dir.is_dir():
        raise FileNotFoundError(
            f"Каталог миграций не найден: {migrations_dir}"
        )

    files_on_disk = _discover_migration_files(migrations_dir)
    _logger.info(
        "Найдено файлов миграций: %d (каталог: %s)",
        len(files_on_disk),
        migrations_dir,
    )

    async with pool.acquire() as conn:
        await conn.execute(_CREATE_META_TABLE_SQL)
        applied_already = await _fetch_applied_versions(conn)

    _check_no_orphan_versions(applied_already, files_on_disk)

    pending = [
        (version, path)
        for version, path in files_on_disk
        if version not in applied_already
    ]

    if not pending:
        _logger.info("Нет миграций для применения, схема актуальна")
        return []

    _logger.info("К применению: %d миграций", len(pending))

    applied_now: list[str] = []
    for version, path in pending:
        await _apply_one_migration(pool, version, path)
        applied_now.append(version)

    return applied_now


def _discover_migration_files(migrations_dir: Path) -> list[tuple[str, Path]]:
    """Список миграционных файлов каталога, отсортированный по номеру."""
    found: list[tuple[str, Path]] = []
    for path in sorted(migrations_dir.iterdir()):
        if not path.is_file() or path.suffix != ".sql":
            continue
        if _MIGRATION_FILENAME_RE.match(path.name) is None:
            raise ValueError(
                f"Имя файла миграции не соответствует формату "
                f"V<NNN>__<name>.sql: {path.name}"
            )
        version = path.stem
        found.append((version, path))
    return found


async def _fetch_applied_versions(conn: asyncpg.Connection) -> set[str]:
    """Множество уже применённых миграций из служебной таблицы."""
    rows = await conn.fetch("SELECT version FROM schema_migrations")
    return {row["version"] for row in rows}


def _check_no_orphan_versions(
    applied: set[str],
    files_on_disk: list[tuple[str, Path]],
) -> None:
    """Убедиться, что для каждой записи в schema_migrations есть файл."""
    on_disk_versions = {version for version, _ in files_on_disk}
    orphans = applied - on_disk_versions
    if orphans:
        raise RuntimeError(
            f"В schema_migrations есть версии, для которых отсутствуют "
            f"файлы на диске: {sorted(orphans)}. Это сигнал повреждения "
            f"истории миграций. Восстанови соответствующие файлы или "
            f"удали записи из schema_migrations вручную."
        )


async def _apply_one_migration(
    pool: asyncpg.Pool,
    version: str,
    path: Path,
) -> None:
    """Применить одну миграцию в отдельной транзакции."""
    sql = path.read_text(encoding="utf-8")
    _logger.info("Применение миграции %s (%s)", version, path.name)
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(sql)
            await conn.execute(
                "INSERT INTO schema_migrations(version) VALUES ($1)",
                version,
            )
    _logger.info("Миграция %s применена", version)


# ---------------------------------------------------------------------------
# CLI: python -m ntrip_accuracy_monitor.persistence.migrator --config config.toml
# ---------------------------------------------------------------------------
def _parse_cli_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m ntrip_accuracy_monitor.persistence.migrator",
        description="Применение миграций схемы PostgreSQL.",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("config.toml"),
        help="Путь к TOML-конфигу приложения (по умолчанию: ./config.toml)",
    )
    return parser.parse_args()


async def _cli_main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    args = _parse_cli_args()
    config = load_config(args.config)
    pool = await create_pool(config.postgres)
    try:
        applied = await apply_migrations(pool)
    finally:
        await close_pool(pool)

    if applied:
        print(f"Применено миграций: {len(applied)}")
        for version in applied:
            print(f"  - {version}")
    else:
        print("Все миграции уже применены.")


if __name__ == "__main__":
    asyncio.run(_cli_main())
