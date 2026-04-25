"""Работа со шкалами времени UTC/GPS/ГЛОНАСС.

Единственная точка получения "сейчас" — now_utc(). В домене и выше
прямой вызов datetime.now() запрещён (приводит к naive datetime и
даёт неопределённый часовой пояс на хосте).

GPS ↔ UTC: реализован "simple offset" — постоянное смещение 18 секунд

ГЛОНАСС: шкала времени ГЛОНАСС внутренне согласована с UTC(SU) —
навигационное сообщение содержит текущие leap-поправки. В NMEA от EFT RS3
время приходит уже в UTC, поэтому отдельный glonass_to_utc() на этом
этапе не реализуем.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

"""Смещение GPS − UTC в секундах. GPS идёт впереди UTC."""
CURRENT_GPS_UTC_LEAP_SECONDS: int = 18
"""Начало шкалы GPS (6 января 1980, 00:00:00 UTC)."""
GPS_EPOCH: datetime = datetime(1980, 1, 6, 0, 0, 0, tzinfo=UTC)
"""ГЛОНАСС системное время = UTC(SU) + 3 часа (московское)."""
GLONASS_EPOCH_OFFSET: timedelta = timedelta(hours=3)
"""
В отличие от GPS, шкала ГЛОНАСС встраивает leap seconds в навсообщение,
поэтому после декодирования сообщения разница с UTC — фиксированные 3 ч.
Константа оставлена для документирования; конверсии пока не нужны.
"""

SECONDS_PER_WEEK: int = 7 * 24 * 60 * 60

type UTCTime = datetime
"""Алиас PEP 695 для явного обозначения TZ-aware UTC datetime.

Важно: Python не проверяет tzinfo через алиас. Защитный вход —
функция ensure_utc() ниже.
"""


def now_utc() -> datetime:
    """Единственное разрешённое место получения "сейчас" в домене."""
    return datetime.now(UTC)


def ensure_utc(dt: datetime) -> datetime:
    """Гарантировать, что datetime в UTC.

    Raises:
        ValueError: если dt naive (tzinfo is None).

    Returns:
        Тот же момент времени в UTC. Если dt уже в UTC — возвращает как есть
        (astimezone — no-op). Если в другой TZ — конвертирует.
    """
    if dt.tzinfo is None:
        raise ValueError(
            "datetime must be timezone-aware (tzinfo required); got naive datetime"
        )
    return dt.astimezone(UTC)


def gps_to_utc(gps_seconds: float, gps_week: int) -> datetime:
    """Преобразовать GPS (week, seconds_of_week) в UTC datetime.

    Args:
        gps_seconds: секунды от начала недели (0..604800).
        gps_week: номер недели GPS (без учёта роллoвера 1024).

    Returns:
        TZ-aware UTC datetime.

    Notes:
        Используется простое смещение CURRENT_GPS_UTC_LEAP_SECONDS
    """
    total_gps_seconds = gps_week * SECONDS_PER_WEEK + gps_seconds
    utc_seconds_since_epoch = total_gps_seconds - CURRENT_GPS_UTC_LEAP_SECONDS
    return GPS_EPOCH + timedelta(seconds=utc_seconds_since_epoch)


def utc_to_gps(dt: datetime) -> tuple[int, float]:
    """Преобразовать UTC datetime в (gps_week, seconds_of_week).

    Args:
        dt: TZ-aware datetime (будет приведён к UTC через ensure_utc).

    Returns:
        (gps_week, seconds_of_week), где seconds_of_week ∈ [0, 604800).
    """
    dt_utc = ensure_utc(dt)
    delta = dt_utc - GPS_EPOCH
    total_gps_seconds = delta.total_seconds() + CURRENT_GPS_UTC_LEAP_SECONDS
    week = int(total_gps_seconds // SECONDS_PER_WEEK)
    sow = total_gps_seconds - week * SECONDS_PER_WEEK
    return week, sow
