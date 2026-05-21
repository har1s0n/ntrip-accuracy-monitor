"""Тесты приоритета поиска config.toml в _locate_config()."""

from __future__ import annotations

from pathlib import Path

import pytest

from ntrip_accuracy_monitor.cli.__main__ import _locate_config


def test_uses_env_var_when_pointing_to_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = tmp_path / "custom.toml"
    config.write_text("")
    monkeypatch.setenv("NAM_CONFIG_PATH", str(config))
    monkeypatch.chdir(tmp_path)

    assert _locate_config() == config


def test_env_var_takes_precedence_over_cwd_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    cwd_config = tmp_path / "config.toml"
    cwd_config.write_text("")
    env_config = tmp_path / "from_env.toml"
    env_config.write_text("")
    monkeypatch.setenv("NAM_CONFIG_PATH", str(env_config))
    monkeypatch.chdir(tmp_path)

    assert _locate_config() == env_config


def test_falls_back_to_cwd_config_toml(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = tmp_path / "config.toml"
    config.write_text("")
    monkeypatch.delenv("NAM_CONFIG_PATH", raising=False)
    monkeypatch.chdir(tmp_path)

    assert _locate_config() == config


def test_env_var_pointing_to_missing_file_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("NAM_CONFIG_PATH", str(tmp_path / "missing.toml"))
    monkeypatch.chdir(tmp_path)

    with pytest.raises(FileNotFoundError, match="does not point to a file"):
        _locate_config()


def test_no_env_and_no_cwd_config_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("NAM_CONFIG_PATH", raising=False)
    monkeypatch.chdir(tmp_path)

    with pytest.raises(FileNotFoundError, match="Configuration file not found"):
        _locate_config()
