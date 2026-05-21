"""Тесты RoverGgaProvider: кэширование GGA от ровера и регенерация сообщений."""

from __future__ import annotations

from datetime import UTC, datetime

from ntrip_accuracy_monitor.protocols.nmea.messages import (
    GgaRecord,
    GstRecord,
    RmcRecord,
)
from ntrip_accuracy_monitor.domain.position import GeodeticPosition
from ntrip_accuracy_monitor.domain.solution_mode import SolutionMode
from ntrip_accuracy_monitor.protocols.ntrip._rover_gga import RoverGgaProvider


def _make_gga(
    *,
    lat: float = 55.7558,
    lon: float = 37.6173,
    alt: float = 200.0,
    mode: SolutionMode = SolutionMode.RTK_FIXED,
    sats: int = 12,
    hdop: float | None = 0.7,
    has_position: bool = True,
) -> GgaRecord:
    return GgaRecord(
        time_utc=datetime(2025, 1, 1, 12, 0, 0, tzinfo=UTC),
        position=(
            GeodeticPosition(latitude_deg=lat, longitude_deg=lon, ellipsoidal_height_m=alt)
            if has_position
            else None
        ),
        solution_mode=mode if has_position else SolutionMode.INVALID,
        satellites_used=sats,
        hdop=hdop,
        age_of_corrections_s=2.0 if mode.is_differential else None,
        reference_station_id=1234 if mode.is_differential else None,
    )


def _xor_checksum(body: bytes) -> int:
    cs = 0
    for ch in body:
        cs ^= ch
    return cs


async def test_initial_state_no_fix() -> None:
    p = RoverGgaProvider()
    assert p.has_fix is False
    assert p.latest_record is None
    assert await p.provide() is None
    assert p.provided_total == 1
    assert p.provided_empty == 1


async def test_consume_non_gga_ignored() -> None:
    p = RoverGgaProvider()
    await p.consume(
        GstRecord(
            time_utc=datetime(2025, 1, 1, 12, 0, 0, tzinfo=UTC),
            sigma_east_m=0.01,
            sigma_north_m=0.01,
            sigma_up_m=0.02,
        )
    )
    await p.consume(
        RmcRecord(
            time_utc=datetime(2025, 1, 1, 12, 0, 0, tzinfo=UTC),
            is_valid=True,
        )
    )
    assert p.has_fix is False
    assert p.consumed_gga_total == 0
    assert await p.provide() is None


async def test_consume_gga_without_position_does_not_set_fix() -> None:
    p = RoverGgaProvider()
    await p.consume(_make_gga(has_position=False))
    assert p.has_fix is False
    assert p.consumed_gga_total == 1
    assert p.consumed_gga_with_fix == 0
    assert await p.provide() is None


async def test_provide_after_valid_fix_returns_valid_gga_bytes() -> None:
    p = RoverGgaProvider()
    await p.consume(_make_gga(lat=55.5, lon=37.5, alt=150.0))
    sentence = await p.provide()

    assert sentence is not None
    assert sentence.startswith(b"$GPGGA,")
    assert sentence.endswith(b"\r\n")

    # Формат: $<body>*XX\r\n — XOR всех байт между $ и * exclusive.
    body = sentence[1:-5]
    cs_hex = sentence[-4:-2]
    assert int(cs_hex, 16) == _xor_checksum(body)


async def test_provide_encodes_cached_coordinates() -> None:
    p = RoverGgaProvider()
    # lat=55.5 → 55°30.0' → "5530.000000,N"
    # lon=37.5 → 37°30.0' → "03730.000000,E"
    await p.consume(_make_gga(lat=55.5, lon=37.5, alt=150.0))
    sentence = await p.provide()
    assert sentence is not None
    assert b"5530.000000,N" in sentence
    assert b"03730.000000,E" in sentence
    assert b"150.0,M" in sentence  # высота из кэша


async def test_provide_uses_latest_fix_after_update() -> None:
    p = RoverGgaProvider()
    await p.consume(_make_gga(lat=55.0, lon=37.0))
    await p.consume(_make_gga(lat=56.0, lon=38.0))

    sentence = await p.provide()
    assert sentence is not None
    # Координаты второй GGA, не первой.
    assert b"5600.000000,N" in sentence
    assert b"03800.000000,E" in sentence


async def test_invalid_gga_after_valid_keeps_cached_fix() -> None:
    p = RoverGgaProvider()
    await p.consume(_make_gga(lat=55.5, lon=37.5))
    await p.consume(_make_gga(has_position=False))  # ровер потерял фикс

    assert p.has_fix is True
    assert p.consumed_gga_total == 2
    assert p.consumed_gga_with_fix == 1

    sentence = await p.provide()
    assert sentence is not None
    assert b"5530.000000,N" in sentence  # позиция от первой, валидной GGA


async def test_provide_encodes_solution_mode_as_quality_int() -> None:
    p = RoverGgaProvider()
    await p.consume(_make_gga(mode=SolutionMode.RTK_FIXED))
    sentence = await p.provide()
    assert sentence is not None
    # quality=4 для RTK_FIXED — поле сразу после долготы и индикатора E/W.
    # Структура: ,<E/W>,<quality>,<sats:02d>,...
    assert b",E,4,12,0.7," in sentence


async def test_provide_uses_default_hdop_when_record_hdop_is_none() -> None:
    p = RoverGgaProvider()
    await p.consume(_make_gga(hdop=None))
    sentence = await p.provide()
    assert sentence is not None
    assert b",1.0," in sentence  # _DEFAULT_HDOP


async def test_counters_reflect_consume_and_provide_activity() -> None:
    p = RoverGgaProvider()
    await p.consume(_make_gga())
    await p.consume(_make_gga(has_position=False))
    await p.provide()
    await p.provide()

    assert p.consumed_gga_total == 2
    assert p.consumed_gga_with_fix == 1
    assert p.provided_total == 2
    assert p.provided_empty == 0
