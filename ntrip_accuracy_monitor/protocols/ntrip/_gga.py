"""NMEA-0183 GGA sentence builder for NTRIP GGA-uplink.

Used to feed VRS-style casters (and GGA-switching enabled non-VRS casters
like the EFT RS3 built-in caster) with a position fix. For real rovers
the GGA should come from the rover stream; this helper exists to bootstrap
from a configured static reference position when no rover is wired up yet.

Reference: NMEA-0183 §6.2.10 (GGA), checksum: XOR of all chars between
'$' and '*' exclusive.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime


def encode_static_gga(
    *,
    lat_deg: float,
    lon_deg: float,
    alt_m: float,
    geoid_sep_m: float = 0.0,
    quality: int = 1,
    sats_used: int = 8,
    hdop: float = 1.0,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> bytes:
    """Build a single GGA sentence with current UTC time and given position.

    Returns bytes ending in CRLF. Suitable for direct write to a TCP socket.
    """
    ts = now()
    if ts.tzinfo is None:
        raise ValueError("now() must return a tz-aware datetime")
    hhmmss = ts.strftime("%H%M%S.") + f"{ts.microsecond // 10000:02d}"

    lat_hemi = "N" if lat_deg >= 0 else "S"
    lat_abs = abs(lat_deg)
    lat_d = int(lat_abs)
    lat_m = (lat_abs - lat_d) * 60.0

    lon_hemi = "E" if lon_deg >= 0 else "W"
    lon_abs = abs(lon_deg)
    lon_d = int(lon_abs)
    lon_m = (lon_abs - lon_d) * 60.0

    body = (
        f"GPGGA,{hhmmss},"
        f"{lat_d:02d}{lat_m:09.6f},{lat_hemi},"
        f"{lon_d:03d}{lon_m:09.6f},{lon_hemi},"
        f"{quality},{sats_used:02d},{hdop:.1f},"
        f"{alt_m:.1f},M,{geoid_sep_m:.1f},M,,"
    )
    checksum = 0
    for ch in body.encode("ascii"):
        checksum ^= ch
    return f"${body}*{checksum:02X}\r\n".encode("ascii")


def static_gga_provider(
    *,
    lat_deg: float,
    lon_deg: float,
    alt_m: float,
    geoid_sep_m: float = 0.0,
) -> Callable[[], Awaitable[bytes | None]]:
    """Adapter: wraps encode_static_gga as a coroutine factory matching
    NtripClient's gga_provider contract. Time is regenerated on each call,
    so even a static GGA gets fresh UTC timestamps for the caster's logs.
    """

    async def _provide() -> bytes | None:
        return encode_static_gga(
            lat_deg=lat_deg, lon_deg=lon_deg,
            alt_m=alt_m, geoid_sep_m=geoid_sep_m,
        )

    return _provide
