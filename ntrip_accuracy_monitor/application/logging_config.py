"""Инициализация логирования приложения.

TODO(): структурное JSON-логирование через stdlib ``logging``:
    - корневой логгер с уровнем из конфига;
    - хэндлеры stderr (и опционально файл с ротацией);
    - форматтер JSON с полями timestamp (UTC), level, logger, message, extra;
    - подавление DEBUG-спама pygnssutils/pynmeagps до WARNING;
    - интеграция с конфигом через ``application.config``.

Сейчас — минимальный каркас на basicConfig.
"""

from __future__ import annotations

import logging


def setup_logging(level: str = "DEBUG") -> None:
    """Настроить корневой логгер приложения.

    Args:
        level: Текстовый уровень (``DEBUG``, ``INFO``, ``WARNING``,
            ``ERROR``, ``CRITICAL``). Регистр не важен.

    Raises:
        ValueError: Если переданный ``level`` не распознаётся stdlib ``logging``.

    Note:
        Текущая реализация — заглушка (basicConfig). Полноценное JSON-
        логирование и обработка ``logging.config.dictConfig`` будут
        добавлены далее.
    """
    numeric_level = logging.getLevelNamesMapping().get(level.upper())
    if numeric_level is None:
        raise ValueError(f"Unknown log level: {level!r}")

    logging.basicConfig(
        level=numeric_level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
