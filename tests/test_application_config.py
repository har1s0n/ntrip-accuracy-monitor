from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from ntrip_accuracy_monitor.application.config import AppConfig, load_config

_MINIMAL_TOML = """
log_level = "DEBUG"

[postgres]
host = "localhost"
port = 5432
database = "ntrip_monitor"
user = "ntrip_user"
min_pool_size = 2
max_pool_size = 10

[caster]
host = "0.0.0.0"
port = 2101
mountpoint = "EFT_BASE"

[[streams]]
stream_id = "base"
host = "192.168.1.10"
port = 9001
role = "base"

[[streams]]
stream_id = "rover_rtk"
host = "192.168.1.11"
port = 9001
role = "rover_rtk"

[reference]
latitude_deg = 55.7558
longitude_deg = 37.6173
ellipsoidal_height_m = 187.5
"""


def _write_toml(tmp_path: Path, content: str) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(content, encoding="utf-8")
    return path


def test_load_config_happy_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PG_PASSWORD", "s3cret")
    cfg = load_config(_write_toml(tmp_path, _MINIMAL_TOML))

    assert isinstance(cfg, AppConfig)
    assert cfg.log_level == "DEBUG"
    assert cfg.postgres.database == "ntrip_monitor"
    assert cfg.postgres.password.get_secret_value() == "s3cret"
    assert len(cfg.streams) == 2
    assert cfg.upstream.enabled is False


def test_missing_pg_password_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("PG_PASSWORD", raising=False)
    with pytest.raises(ValueError, match="PG_PASSWORD"):
        load_config(_write_toml(tmp_path, _MINIMAL_TOML))


def test_duplicate_stream_ids_raise(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PG_PASSWORD", "s3cret")
    duplicated = _MINIMAL_TOML.replace(
        'stream_id = "rover_rtk"', 'stream_id = "base"'
    )
    with pytest.raises(ValidationError, match="unique"):
        load_config(_write_toml(tmp_path, duplicated))


def test_pool_sizes_inconsistent_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PG_PASSWORD", "s3cret")
    broken = _MINIMAL_TOML.replace("min_pool_size = 2", "min_pool_size = 20")
    with pytest.raises(ValidationError, match="pool_size"):
        load_config(_write_toml(tmp_path, broken))


def test_empty_streams_list_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PG_PASSWORD", "s3cret")
    no_streams = """
log_level = "INFO"

[postgres]
database = "db"
user = "u"

[caster]
mountpoint = "M"

streams = []

[reference]
latitude_deg = 0.0
longitude_deg = 0.0
ellipsoidal_height_m = 0.0
"""
    with pytest.raises(ValidationError):
        load_config(_write_toml(tmp_path, no_streams))


def test_upstream_enabled_requires_url_and_mountpoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PG_PASSWORD", "s3cret")
    with_broken_upstream = _MINIMAL_TOML + """
[upstream]
enabled = true
"""
    with pytest.raises(ValidationError, match="url"):
        load_config(_write_toml(tmp_path, with_broken_upstream))


def test_password_not_leaked_in_repr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PG_PASSWORD", "top_secret_value")
    cfg = load_config(_write_toml(tmp_path, _MINIMAL_TOML))
    assert "top_secret_value" not in repr(cfg)
    assert "top_secret_value" not in str(cfg.postgres)


_UPSTREAM_ENABLED_TOML = _MINIMAL_TOML + """
[upstream]
enabled = true
url = "http://rtk2go.com:2101"
mountpoint = "RTK2GO_1"
user = "user@example.com"
"""


def test_ntrip_upstream_password_from_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PG_PASSWORD", "pg_secret")
    monkeypatch.setenv("NTRIP_UPSTREAM_PASSWORD", "ntrip_secret")

    cfg = load_config(_write_toml(tmp_path, _UPSTREAM_ENABLED_TOML))

    assert cfg.upstream.enabled is True
    assert cfg.upstream.password is not None
    assert cfg.upstream.password.get_secret_value() == "ntrip_secret"


def test_ntrip_upstream_password_absent_is_ok_when_not_provided(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Upstream может работать без пароля (анонимный кастер)."""
    monkeypatch.setenv("PG_PASSWORD", "pg_secret")
    monkeypatch.delenv("NTRIP_UPSTREAM_PASSWORD", raising=False)

    cfg = load_config(_write_toml(tmp_path, _UPSTREAM_ENABLED_TOML))

    assert cfg.upstream.enabled is True
    assert cfg.upstream.password is None


def test_password_in_toml_is_ignored_postgres(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Пароль PG в TOML молча отбрасывается, используется env."""
    monkeypatch.setenv("PG_PASSWORD", "env_wins")
    toml_with_pg_password = _MINIMAL_TOML.replace(
        "user = \"ntrip_user\"",
        "user = \"ntrip_user\"\npassword = \"toml_loses\"",
    )

    cfg = load_config(_write_toml(tmp_path, toml_with_pg_password))

    assert cfg.postgres.password.get_secret_value() == "env_wins"


def test_password_in_toml_is_ignored_upstream(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Пароль upstream в TOML отбрасывается, даже если env не задан."""
    monkeypatch.setenv("PG_PASSWORD", "pg_secret")
    monkeypatch.delenv("NTRIP_UPSTREAM_PASSWORD", raising=False)

    toml_with_upstream_password = _UPSTREAM_ENABLED_TOML + 'password = "toml_loses"\n'

    cfg = load_config(_write_toml(tmp_path, toml_with_upstream_password))

    # env не задан → пароль None; TOML-значение проигнорировано
    assert cfg.upstream.password is None


def test_stream_port_defaults_to_9001(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Если port не указан в TOML, используется штатный для EFT RS3 — 9001."""
    monkeypatch.setenv("PG_PASSWORD", "s3cret")
    # В _MINIMAL_TOML port указан явно; уберём его для одного из стримов.
    toml_without_port = """
log_level = "INFO"

[postgres]
database = "ntrip_monitor"
user = "ntrip_user"

[caster]
mountpoint = "EFT_BASE"

[[streams]]
stream_id = "base"
host = "192.168.1.10"
role = "base"

[reference]
latitude_deg = 0.0
longitude_deg = 0.0
ellipsoidal_height_m = 0.0
"""
    cfg = load_config(_write_toml(tmp_path, toml_without_port))
    assert cfg.streams[0].port == 9001
