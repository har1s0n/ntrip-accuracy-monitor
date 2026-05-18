"""Слой персистентности: подключение к PostgreSQL, миграции и репозитории.

Функция ``apply_migrations`` доступна через прямой импорт, чтобы запуск
миграций через ``python -m`` не сталкивался с предупреждением Python
о двойной инициализации модуля::

    from ntrip_accuracy_monitor.persistence.migrator import apply_migrations
"""

from ntrip_accuracy_monitor.persistence.epoch_repository import EpochRepository
from ntrip_accuracy_monitor.persistence.pool import close_pool, create_pool
from ntrip_accuracy_monitor.persistence.rtcm_repository import (
    RtcmMessageRecord,
    RtcmRepository,
)
from ntrip_accuracy_monitor.persistence.session_repository import (
    SessionRepository,
    SessionRow,
)

__all__ = [
    "EpochRepository",
    "RtcmMessageRecord",
    "RtcmRepository",
    "SessionRepository",
    "SessionRow",
    "close_pool",
    "create_pool",
]
