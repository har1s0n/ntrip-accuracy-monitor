"""Координатные типы домена.

Здесь только value-типы для хранения и базовая валидация диапазонов.
"""

from __future__ import annotations

from dataclasses import dataclass
from math import sqrt


@dataclass(frozen=True, slots=True)
class GeodeticPosition:
    """Точка в геодезических координатах WGS-84.

    Attributes:
        latitude_deg: широта в градусах, ∈ [-90, 90].
        longitude_deg: долгота в градусах, ∈ [-180, 180].
        ellipsoidal_height_m: эллипсоидальная высота в метрах,
            ∈ [-1000, 20000] — защитный фильтр от мусора из парсера.
    """

    latitude_deg: float
    longitude_deg: float
    ellipsoidal_height_m: float

    def __post_init__(self) -> None:
        if not -90.0 <= self.latitude_deg <= 90.0:
            raise ValueError(
                f"latitude_deg out of range [-90, 90]: {self.latitude_deg!r}"
            )
        if not -180.0 <= self.longitude_deg <= 180.0:
            raise ValueError(
                f"longitude_deg out of range [-180, 180]: {self.longitude_deg!r}"
            )
        if not -1000.0 <= self.ellipsoidal_height_m <= 20000.0:
            raise ValueError(
                "ellipsoidal_height_m out of plausible range [-1000, 20000]: "
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
