"""Проверка преобразований WGS-84 → ECEF → ENU.

Используются аналитические смещения по широте/долготе/высоте, точность
которых для маленьких смещений (метры) известна из определений WGS-84.
Внешние библиотеки (pyproj) намеренно не используются — тест проверяет
формулу, а не другую её реализацию.
"""

from __future__ import annotations

import math

import pytest

from ntrip_accuracy_monitor.domain.geodetic import (
    EnuTransformer,
    geodetic_to_ecef,
)
from ntrip_accuracy_monitor.domain.position import GeodeticPosition

# Москва, ARP антенны №2 из antenna_2_arp_wgs84.toml.
_REFERENCE = GeodeticPosition(
    latitude_deg=55.984304296,
    longitude_deg=37.213667733,
    ellipsoidal_height_m=220.7379,
)


def test_geodetic_to_ecef_matches_known_values() -> None:
    """ECEF координаты эталонной точки совпадают с RTKLIB-расчётом
    из antenna_2_arp_wgs84.toml в пределах 1 мм."""
    ecef = geodetic_to_ecef(_REFERENCE)
    assert ecef.x_m == pytest.approx(2848205.3732, abs=1e-3)
    assert ecef.y_m == pytest.approx(2162976.6207, abs=1e-3)
    assert ecef.z_m == pytest.approx(5263647.7662, abs=1e-3)


def test_enu_at_reference_is_origin() -> None:
    """Эталонная точка в собственной системе ENU == (0, 0, 0)."""
    transformer = EnuTransformer.at(_REFERENCE)
    offset = transformer.to_enu(_REFERENCE)
    assert offset.east_m == pytest.approx(0.0, abs=1e-6)
    assert offset.north_m == pytest.approx(0.0, abs=1e-6)
    assert offset.up_m == pytest.approx(0.0, abs=1e-6)


def test_enu_height_only_offset() -> None:
    """Точка строго над эталоном на +10 м даёт ENU ≈ (0, 0, 10)."""
    transformer = EnuTransformer.at(_REFERENCE)
    above = GeodeticPosition(
        latitude_deg=_REFERENCE.latitude_deg,
        longitude_deg=_REFERENCE.longitude_deg,
        ellipsoidal_height_m=_REFERENCE.ellipsoidal_height_m + 10.0,
    )
    offset = transformer.to_enu(above)
    assert offset.east_m == pytest.approx(0.0, abs=1e-6)
    assert offset.north_m == pytest.approx(0.0, abs=1e-6)
    assert offset.up_m == pytest.approx(10.0, abs=1e-6)


def test_enu_small_latitude_offset_maps_to_north() -> None:
    """Малое смещение по широте (без изменения долготы и высоты) даёт
    почти чистое смещение по N. Длина дуги меридиана для 1° ≈ 111 320 м
    на широте Москвы; для 0.00001° ожидаем ~1.113 м."""
    transformer = EnuTransformer.at(_REFERENCE)
    delta_lat_deg = 1e-5
    moved = GeodeticPosition(
        latitude_deg=_REFERENCE.latitude_deg + delta_lat_deg,
        longitude_deg=_REFERENCE.longitude_deg,
        ellipsoidal_height_m=_REFERENCE.ellipsoidal_height_m,
    )
    offset = transformer.to_enu(moved)
    # Геометрическая верификация: длина дуги по меридиану на этой широте.
    # M (радиус кривизны меридиана) = a(1 - e²) / (1 - e² sin²φ)^(3/2).
    lat_rad = math.radians(_REFERENCE.latitude_deg)
    a = 6378137.0
    e2 = 1.0 / 298.257223563 * (2.0 - 1.0 / 298.257223563)
    m_radius = a * (1.0 - e2) / (1.0 - e2 * math.sin(lat_rad) ** 2) ** 1.5
    expected_north_m = math.radians(delta_lat_deg) * m_radius
    assert offset.north_m == pytest.approx(expected_north_m, rel=1e-4)
    assert offset.east_m == pytest.approx(0.0, abs=1e-4)
    assert offset.up_m == pytest.approx(0.0, abs=1e-4)


def test_enu_small_longitude_offset_maps_to_east() -> None:
    """Малое смещение по долготе даёт почти чистое смещение по E.
    Длина дуги по параллели = N cos(φ) · Δλ_рад."""
    transformer = EnuTransformer.at(_REFERENCE)
    delta_lon_deg = 1e-5
    moved = GeodeticPosition(
        latitude_deg=_REFERENCE.latitude_deg,
        longitude_deg=_REFERENCE.longitude_deg + delta_lon_deg,
        ellipsoidal_height_m=_REFERENCE.ellipsoidal_height_m,
    )
    offset = transformer.to_enu(moved)
    lat_rad = math.radians(_REFERENCE.latitude_deg)
    a = 6378137.0
    e2 = 1.0 / 298.257223563 * (2.0 - 1.0 / 298.257223563)
    n_radius = a / math.sqrt(1.0 - e2 * math.sin(lat_rad) ** 2)
    expected_east_m = math.radians(delta_lon_deg) * n_radius * math.cos(lat_rad)
    assert offset.east_m == pytest.approx(expected_east_m, rel=1e-4)
    assert offset.north_m == pytest.approx(0.0, abs=1e-4)
    assert offset.up_m == pytest.approx(0.0, abs=1e-4)


def test_enu_antisymmetry_under_reference_swap() -> None:
    """Если поменять эталон и точку местами, ENU-смещение меняет знак."""
    other = GeodeticPosition(
        latitude_deg=_REFERENCE.latitude_deg + 1e-4,
        longitude_deg=_REFERENCE.longitude_deg + 1e-4,
        ellipsoidal_height_m=_REFERENCE.ellipsoidal_height_m + 5.0,
    )
    forward = EnuTransformer.at(_REFERENCE).to_enu(other)
    backward = EnuTransformer.at(other).to_enu(_REFERENCE)
    # На метровом масштабе ENU-разница между «прямым» и «обратным»
    # сравнением должна быть ниже 1 мм (на больших — кривизна Земли
    # перестаёт быть линейной, и тождество приближённое).
    assert forward.east_m == pytest.approx(-backward.east_m, abs=1e-3)
    assert forward.north_m == pytest.approx(-backward.north_m, abs=1e-3)
    assert forward.up_m == pytest.approx(-backward.up_m, abs=1e-3)


def test_enu_total_error_property_consistent() -> None:
    """ENUOffset.total_error_m совпадает с sqrt(E² + N² + U²)."""
    transformer = EnuTransformer.at(_REFERENCE)
    other = GeodeticPosition(
        latitude_deg=_REFERENCE.latitude_deg + 1e-4,
        longitude_deg=_REFERENCE.longitude_deg + 2e-4,
        ellipsoidal_height_m=_REFERENCE.ellipsoidal_height_m + 3.0,
    )
    offset = transformer.to_enu(other)
    expected = math.sqrt(
        offset.east_m ** 2 + offset.north_m ** 2 + offset.up_m ** 2
    )
    assert offset.total_error_m == pytest.approx(expected, rel=1e-12)
