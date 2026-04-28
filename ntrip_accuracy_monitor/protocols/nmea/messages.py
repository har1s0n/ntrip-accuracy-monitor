"""Доменные представления NMEA-сообщений.

Отделяют сырые pynmeagps-объекты (со строковыми полями вида
"5547.43580" + индикатор N/S и т.п.) от остальной системы.
Выше этого пакета классы NMEAMessage не должны просачиваться.
"""

from dataclasses import dataclass
from datetime import datetime

from ntrip_accuracy_monitor.domain.position import GeodeticPosition
from ntrip_accuracy_monitor.domain.solution_mode import SolutionMode


@dataclass(frozen=True, slots=True)
class GgaRecord:
    """Position fix: $xxGGA.

        `position is None` допустимо только при solution_mode == INVALID
        (приёмник без фикса). При любом другом solution_mode пустая позиция
        в исходном сообщении трактуется как ошибка формата.
        """

    time_utc: datetime
    position: GeodeticPosition | None
    solution_mode: SolutionMode
    satellites_used: int
    hdop: float | None
    age_of_corrections_s: float | None
    reference_station_id: int | None


@dataclass(frozen=True, slots=True)
class GstRecord:
    """1σ оценки ошибок: $xxGST.

    Маппинг: pynmeagps извлекает stdLat/stdLong/stdAlt уже в метрах
    (поля 6/7/8 GST по NMEA-0183). Перекладываем под ENU-имена с
    допущением, что для малых отклонений (порядка σ/R_Earth ≈ 1e-7)
    разница между геодезической рамкой широты/долготы и
    топоцентрической ENU пренебрежимо мала.

    Кросс-корреляции (поля 3-5: полуоси и азимут эллипса ошибок)
    в этой версии игнорируются — проект работает с диагональными σ.
    """

    time_utc: datetime
    sigma_east_m: float
    sigma_north_m: float
    sigma_up_m: float


@dataclass(frozen=True, slots=True)
class GsaRecord:
    """DOPы и состав созвездия в фиксе: $xxGSA.

    fix_type: 1 = no fix, 2 = 2D, 3 = 3D.
    """

    pdop: float | None
    hdop: float | None
    vdop: float | None
    fix_type: int
    satellite_prns: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class RmcRecord:
    """Recommended Minimum Course: $xxRMC.

    Используется как источник полной даты для сшивки с GGA
    (у GGA только HH:MM:SS, без даты).
    """

    time_utc: datetime
    is_valid: bool


@dataclass(frozen=True, slots=True)
class ZdaRecord:
    """UTC date/time: $xxZDA. Альтернативный источник даты для GGA."""

    time_utc: datetime


type NmeaRecord = GgaRecord | GstRecord | GsaRecord | RmcRecord | ZdaRecord
