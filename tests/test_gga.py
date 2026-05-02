from __future__ import annotations

from datetime import UTC, datetime

import pytest

from ntrip_accuracy_monitor.protocols.ntrip._gga import (
    encode_static_gga,
    static_gga_provider,
)


def _fake_now() -> datetime:
    return datetime(2026, 5, 1, 12, 0, 0, 250000, tzinfo=UTC)


def test_encode_static_gga_warsaw() -> None:
    sentence = encode_static_gga(
        lat_deg=52.2297, lon_deg=21.0122, alt_m=110.0,
        geoid_sep_m=37.0, now=_fake_now,
    )
    text = sentence.decode("ascii")
    # Structure: starts with '$GPGGA,', ends with '*HH\r\n'.
    assert text.startswith("$GPGGA,120000.25,")
    assert text.endswith("\r\n")
    body, checksum = text[1:-5].split("*", 1) if False else (text[1:-5], text[-4:-2])
    # Recompute checksum and compare.
    expected = 0
    for ch in (text[1:].split("*", 1)[0]).encode("ascii"):
        expected ^= ch
    assert int(text.split("*", 1)[1][:2], 16) == expected
    # Coordinates: ddmm.mmmmmm, lat in N, lon in E.
    assert ",5213.782000,N," in text
    assert ",02100.732000,E," in text
    assert ",1,08,1.0,110.0,M,37.0,M,," in text


def test_encode_static_gga_southwest_quadrant() -> None:
    # Negative lat → S, negative lon → W.
    sentence = encode_static_gga(
        lat_deg=-33.8688, lon_deg=-58.3816, alt_m=25.0, now=_fake_now,
    )
    text = sentence.decode("ascii")
    assert ",S," in text
    assert ",W," in text


def test_encode_static_gga_rejects_naive_datetime() -> None:
    with pytest.raises(ValueError, match="tz-aware"):
        encode_static_gga(
            lat_deg=0.0, lon_deg=0.0, alt_m=0.0,
            now=lambda: datetime(2026, 1, 1),  # naive
        )


@pytest.mark.asyncio
async def test_static_gga_provider_returns_bytes() -> None:
    provider = static_gga_provider(lat_deg=52.0, lon_deg=21.0, alt_m=100.0)
    out = await provider()
    assert out is not None
    assert out.startswith(b"$GPGGA,")
    assert out.endswith(b"\r\n")
