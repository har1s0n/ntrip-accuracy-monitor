from __future__ import annotations

import sys
from pathlib import Path

import pytest

from ntrip_accuracy_monitor.cli.__main__ import _parse_args, _resolve_config_path


def test_parse_args_run(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "argv", ["ntrip-accuracy-monitor", "run"])
    args = _parse_args()
    assert args.command == "run"
    assert args.config is None
    assert args.log_level is None


def test_parse_args_global_options(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        sys, "argv",
        ["ntrip-accuracy-monitor", "--config", "x.toml",
         "--log-level", "DEBUG", "run"],
    )
    args = _parse_args()
    assert args.command == "run"
    assert args.config == "x.toml"
    assert args.log_level == "DEBUG"


def test_parse_args_requires_subcommand(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "argv", ["ntrip-accuracy-monitor"])
    with pytest.raises(SystemExit):
        _parse_args()


def test_resolve_config_path_flag_wins(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = tmp_path / "my.toml"
    cfg.write_text("", encoding="utf-8")
    monkeypatch.setenv("NAM_CONFIG_PATH", str(tmp_path / "other.toml"))
    assert _resolve_config_path(str(cfg)) == cfg


def test_resolve_config_path_flag_missing(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        _resolve_config_path(str(tmp_path / "nope.toml"))
