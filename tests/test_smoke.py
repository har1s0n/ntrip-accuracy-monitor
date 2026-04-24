"""Smoke-тесты: asyncio-рантайм жив, внутренние пакеты и внешние зависимости импортируются."""

from __future__ import annotations

import asyncio
import importlib

INTERNAL_PACKAGES: tuple[str, ...] = (
    "ntrip_accuracy_monitor",
    "ntrip_accuracy_monitor.transport",
    "ntrip_accuracy_monitor.protocols",
    "ntrip_accuracy_monitor.domain",
    "ntrip_accuracy_monitor.persistence",
    "ntrip_accuracy_monitor.metrics",
    "ntrip_accuracy_monitor.application",
    "ntrip_accuracy_monitor.cli",
)

EXTERNAL_PACKAGES: tuple[str, ...] = (
    "asyncpg",
    "pydantic",
    "pynmeagps",
    "pyrtcm",
    "pygnssutils",
    "pyproj",
    "numpy",
)


async def test_asyncio_runtime_alive() -> None:
    """pytest-asyncio сконфигурирован (asyncio_mode=auto) и луп запускается."""
    await asyncio.sleep(0)


def test_internal_packages_importable() -> None:
    """Все публичные пакеты приложения импортируются без ошибок."""
    for name in INTERNAL_PACKAGES:
        importlib.import_module(name)


def test_external_dependencies_importable() -> None:
    """Внешние зависимости установлены и импортируются (без проверки версии)."""
    for name in EXTERNAL_PACKAGES:
        importlib.import_module(name)
