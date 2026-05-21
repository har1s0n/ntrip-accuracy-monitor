"""Тесты расширений AppConfig: GGA-uplink источник и CapturesConfig.

Тесты обходят load_config() и работают напрямую с pydantic-валидацией
через AppConfig.model_validate — env-переменные не требуются.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from pydantic import SecretStr, ValidationError

from ntrip_accuracy_monitor.application.config import (
    AppConfig,
    CapturesConfig,
    UpstreamNtripConfig,
)


def _base_payload(**overrides: Any) -> dict[str, Any]:
    """Минимально-валидный конфиг для AppConfig.model_validate."""
    payload: dict[str, Any] = {
        "postgres": {
            "database": "nam",
            "user": "nam",
            "password": SecretStr("secret"),
        },
        "local_caster": {
            "mountpoint": "TEST",
        },
        "nmea_receivers": [
            {
                "receiver_id": "rover_a",
                "host": "10.0.0.10",
                "role": "rover_rtk",
            },
            {
                "receiver_id": "rover_b",
                "host": "10.0.0.11",
                "role": "rover_spp",
            },
            {
                "receiver_id": "base",
                "host": "10.0.0.12",
                "role": "base",
            },
        ],
        "reference_antenna": {
            "latitude_deg": 52.0,
            "longitude_deg": 21.0,
            "ellipsoidal_height_m": 150.0,
        },
    }
    payload.update(overrides)
    return payload


# --------------------------------------------------------------------------
# Дефолты — обратная совместимость со старыми конфигами
# --------------------------------------------------------------------------
def test_defaults_when_new_fields_omitted() -> None:
    cfg = AppConfig.model_validate(_base_payload())
    assert cfg.upstream_ntrip.gga_source_receiver_id is None
    assert cfg.upstream_ntrip.gga_interval_s == 10.0
    assert cfg.captures.enabled is False
    assert cfg.captures.directory == Path("./captures")


# --------------------------------------------------------------------------
# UpstreamNtripConfig.gga_interval_s
# --------------------------------------------------------------------------
def test_gga_interval_s_must_be_positive() -> None:
    with pytest.raises(ValidationError, match="greater than 0"):
        UpstreamNtripConfig(gga_interval_s=0.0)
    with pytest.raises(ValidationError, match="greater than 0"):
        UpstreamNtripConfig(gga_interval_s=-1.0)


def test_gga_interval_s_accepts_custom_value() -> None:
    cfg = UpstreamNtripConfig(gga_interval_s=30.0)
    assert cfg.gga_interval_s == 30.0


# --------------------------------------------------------------------------
# AppConfig cross-field валидация GGA-источника
# --------------------------------------------------------------------------
def test_gga_source_receiver_id_resolves_to_rover_rtk() -> None:
    cfg = AppConfig.model_validate(
        _base_payload(
            upstream_ntrip={
                "enabled": True,
                "url": "http://caster.example.com:2101",
                "mountpoint": "VRS_RTCM3",
                "gga_source_receiver_id": "rover_a",
            },
        )
    )
    assert cfg.upstream_ntrip.gga_source_receiver_id == "rover_a"


def test_gga_source_receiver_id_resolves_to_rover_spp() -> None:
    cfg = AppConfig.model_validate(
        _base_payload(
            upstream_ntrip={
                "enabled": True,
                "url": "http://caster.example.com:2101",
                "mountpoint": "VRS_RTCM3",
                "gga_source_receiver_id": "rover_b",
            },
        )
    )
    assert cfg.upstream_ntrip.gga_source_receiver_id == "rover_b"


def test_gga_source_unknown_receiver_id_rejected() -> None:
    with pytest.raises(ValidationError, match="not found in nmea_receivers"):
        AppConfig.model_validate(
            _base_payload(
                upstream_ntrip={
                    "enabled": True,
                    "url": "http://caster.example.com:2101",
                    "mountpoint": "VRS_RTCM3",
                    "gga_source_receiver_id": "ghost",
                },
            )
        )


def test_gga_source_with_base_role_rejected() -> None:
    with pytest.raises(ValidationError, match="must be 'rover_rtk' or 'rover_spp'"):
        AppConfig.model_validate(
            _base_payload(
                upstream_ntrip={
                    "enabled": True,
                    "url": "http://caster.example.com:2101",
                    "mountpoint": "VRS_RTCM3",
                    "gga_source_receiver_id": "base",
                },
            )
        )


def test_gga_source_not_validated_when_upstream_disabled() -> None:
    """enabled=False — GGA-источник не проверяется, можно держать чорновое
    значение receiver_id в TOML без удаления."""
    cfg = AppConfig.model_validate(
        _base_payload(
            upstream_ntrip={
                "enabled": False,
                "gga_source_receiver_id": "ghost",  # не существует — но ок
            },
        )
    )
    assert cfg.upstream_ntrip.enabled is False
    assert cfg.upstream_ntrip.gga_source_receiver_id == "ghost"


# --------------------------------------------------------------------------
# CapturesConfig
# --------------------------------------------------------------------------
def test_captures_defaults_disabled() -> None:
    cfg = CapturesConfig()
    assert cfg.enabled is False
    assert cfg.directory == Path("./captures")


def test_captures_enabled_with_custom_directory() -> None:
    cfg = CapturesConfig.model_validate(
        {"enabled": True, "directory": "/var/log/nam/captures"}
    )
    assert cfg.enabled is True
    assert cfg.directory == Path("/var/log/nam/captures")
    assert isinstance(cfg.directory, Path)


def test_captures_section_integrates_into_app_config() -> None:
    cfg = AppConfig.model_validate(
        _base_payload(captures={"enabled": True, "directory": "./out"})
    )
    assert cfg.captures.enabled is True
    assert cfg.captures.directory == Path("./out")
