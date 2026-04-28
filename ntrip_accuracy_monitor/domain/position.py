"""Координатные типы домена.

Здесь только value-типы для хранения и базовая валидация диапазонов.
"""

from __future__ import annotations

from dataclasses import dataclass
from math import sqrt
from typing import Final

_MIN_LATITUDE_DEG: Final[float] = -90.0
_MAX_LATITUDE_DEG: Final[float] = 90.0
_MIN_LONGITUDE_DEG: Final[float] = -180.0
_MAX_LONGITUDE_DEG: Final[float] = 180.0
_MIN_PLAUSIBLE_HEIGHT_M: Final[float] = -1000.0
_MAX_PLAUSIBLE_HEIGHT_M: Final[float] = 30000.0


@dataclass(frozen=True, slots=True)
class GeodeticPosition:
    """Точка в геодезических координатах WGS-84.

    Attributes:
        latitude_deg: широта в градусах, ∈ [-90, 90].
        longitude_deg: долгота в градусах, ∈ [-180, 180].
        ellipsoidal_height_m: эллипсоидальная высота в метрах,
            ∈ [-1000, 30000] — защитный фильтр от мусора из парсера.
    """

    latitude_deg: float
    longitude_deg: float
    ellipsoidal_height_m: float

    def __post_init__(self) -> None:
        if not _MIN_LATITUDE_DEG <= self.latitude_deg <= _MAX_LATITUDE_DEG:
            raise ValueError(
                f"latitude_deg out of range [-90, 90]: {self.latitude_deg!r}"
            )
        if not _MIN_LONGITUDE_DEG <= self.longitude_deg <= _MAX_LONGITUDE_DEG:
            raise ValueError(
                f"longitude_deg out of range [-180, 180]: {self.longitude_deg!r}"
            )
        if not _MIN_PLAUSIBLE_HEIGHT_M <= self.ellipsoidal_height_m <= _MAX_PLAUSIBLE_HEIGHT_M:
            raise ValueError(
                "ellipsoidal_height_m out of plausible range [-1000, 30000]: "
                f"{self.ellipsoidal_height_m!r}"
            )


@dataclass(frozen=True, slots=True)
class ENUOffset:
    """Вектор смещения в локальной топоцентрической системе ENU (метры).

    E — East, N — North, U — Up. Все значения в метрах; миллиметры и
    другие единицы запрещены на границе домена.
    """

    east_m: float
    north_m: float
    up_m: float

    @property
    def horizontal_error_m(self) -> float:
        r"""Горизонтальная радиальная ошибка: $\sqrt{E^2 + N^2}$."""
        return sqrt(self.east_m * self.east_m + self.north_m * self.north_m)

    @property
    def total_error_m(self) -> float:
        r"""Полная 3D-ошибка: $\sqrt{E^2 + N^2 + U^2}$."""
        return sqrt(
            self.east_m * self.east_m
            + self.north_m * self.north_m
            + self.up_m * self.up_m
        )
