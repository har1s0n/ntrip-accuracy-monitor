"""Преобразования координат WGS-84 → ECEF → ENU.

Используется для расчёта метрик точности позиционирования в локальной
топоцентрической системе координат (East-North-Up) относительно
эталонной точки антенны.

Формулы стандартные:
  - geodetic → ECEF: Heiskanen & Moritz, "Physical Geodesy" (1967),
    уравнения (2-141), эллипсоид WGS-84 без datum-конверсии.
  - ECEF → ENU: ортогональная матрица поворота 3x3, центрированная
    на эталонной геодезической точке.

Внешние библиотеки (pyproj/proj) сознательно не используются — задача
для одного эллипсоида тривиальна и легче тестируется без зависимостей.

Шаблон использования:

    transformer = EnuTransformer.at(reference_position)
    for epoch in epochs:
        offset = transformer.to_enu(epoch.position)
        # offset.east_m, offset.north_m, offset.up_m
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Final, Self

from ntrip_accuracy_monitor.domain.position import ENUOffset, GeodeticPosition

# WGS-84 эллипсоид (NIMA TR8350.2, World Geodetic System 1984).
_WGS84_A_M: Final[float] = 6378137.0
"""Большая полуось эллипсоида, метры."""

_WGS84_F: Final[float] = 1.0 / 298.257223563
"""Сжатие эллипсоида."""

_WGS84_E_SQUARED: Final[float] = _WGS84_F * (2.0 - _WGS84_F)
"""Первый эксцентриситет в квадрате: e² = 2f − f²."""


@dataclass(frozen=True, slots=True)
class _EcefPoint:
    """Точка в геоцентрической системе ECEF, метры. Внутренний тип."""

    x_m: float
    y_m: float
    z_m: float


def geodetic_to_ecef(position: GeodeticPosition) -> _EcefPoint:
    r"""LLH (WGS-84) → ECEF.

    Формулы:
      $N = a / \sqrt{1 - e^2 \sin^2 \varphi}$
      $X = (N + h) \cos\varphi \cos\lambda$
      $Y = (N + h) \cos\varphi \sin\lambda$
      $Z = (N(1 - e^2) + h) \sin\varphi$
    """
    lat_rad = math.radians(position.latitude_deg)
    lon_rad = math.radians(position.longitude_deg)
    h_m = position.ellipsoidal_height_m

    sin_lat = math.sin(lat_rad)
    cos_lat = math.cos(lat_rad)
    sin_lon = math.sin(lon_rad)
    cos_lon = math.cos(lon_rad)

    n_radius_m = _WGS84_A_M / math.sqrt(1.0 - _WGS84_E_SQUARED * sin_lat * sin_lat)

    x_m = (n_radius_m + h_m) * cos_lat * cos_lon
    y_m = (n_radius_m + h_m) * cos_lat * sin_lon
    z_m = (n_radius_m * (1.0 - _WGS84_E_SQUARED) + h_m) * sin_lat

    return _EcefPoint(x_m=x_m, y_m=y_m, z_m=z_m)


@dataclass(frozen=True, slots=True)
class EnuTransformer:
    """Преобразователь geodetic → ENU, привязанный к эталонной точке.

    Эталонная точка задаётся один раз через :meth:`at`; матрица поворота
    ECEF → ENU и ECEF-координаты эталона кэшируются. Каждый вызов
    :meth:`to_enu` — O(1), без повторного расчёта тригонометрии для
    эталона. Это важно для расчёта метрик по большим выборкам (десятки
    тысяч эпох).

    Атрибуты с префиксом «_» — внутреннее состояние; модифицировать
    извне нельзя (dataclass frozen).
    """

    _ref_ecef: _EcefPoint
    _ref_sin_lat: float
    _ref_cos_lat: float
    _ref_sin_lon: float
    _ref_cos_lon: float

    @classmethod
    def at(cls, reference: GeodeticPosition) -> Self:
        """Построить преобразователь, центрированный на ``reference``."""
        ref_ecef = geodetic_to_ecef(reference)
        lat_rad = math.radians(reference.latitude_deg)
        lon_rad = math.radians(reference.longitude_deg)
        return cls(
            _ref_ecef=ref_ecef,
            _ref_sin_lat=math.sin(lat_rad),
            _ref_cos_lat=math.cos(lat_rad),
            _ref_sin_lon=math.sin(lon_rad),
            _ref_cos_lon=math.cos(lon_rad),
        )

    def to_enu(self, target: GeodeticPosition) -> ENUOffset:
        r"""Вернуть смещение ``target`` относительно эталона в системе ENU.

        Алгоритм:
          1. ``target`` → ECEF;
          2. вектор $\Delta = X_{target} - X_{ref}$ в ECEF;
          3. поворот в локальную ENU матрицей:
             $\begin{pmatrix} E \\ N \\ U \end{pmatrix}
              = R \cdot \Delta$, где
             $R = \begin{pmatrix}
                  -\sin\lambda & \cos\lambda & 0 \\
                  -\sin\varphi\cos\lambda & -\sin\varphi\sin\lambda & \cos\varphi \\
                  \cos\varphi\cos\lambda & \cos\varphi\sin\lambda & \sin\varphi
                  \end{pmatrix}$
             ($\varphi, \lambda$ — широта/долгота эталона).
        """
        target_ecef = geodetic_to_ecef(target)
        dx_m = target_ecef.x_m - self._ref_ecef.x_m
        dy_m = target_ecef.y_m - self._ref_ecef.y_m
        dz_m = target_ecef.z_m - self._ref_ecef.z_m

        east_m = -self._ref_sin_lon * dx_m + self._ref_cos_lon * dy_m
        north_m = (
            -self._ref_sin_lat * self._ref_cos_lon * dx_m
            - self._ref_sin_lat * self._ref_sin_lon * dy_m
            + self._ref_cos_lat * dz_m
        )
        up_m = (
            self._ref_cos_lat * self._ref_cos_lon * dx_m
            + self._ref_cos_lat * self._ref_sin_lon * dy_m
            + self._ref_sin_lat * dz_m
        )

        return ENUOffset(east_m=east_m, north_m=north_m, up_m=up_m)
