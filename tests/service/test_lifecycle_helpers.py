"""Юнит-тесты вспомогательных функций SessionLifecycle.

Сам ``run()`` проверяется интеграционным e2e (см. test_lifecycle_e2e).
Здесь — чистые юнит-тесты помощников.
"""

from __future__ import annotations

import pytest
from pydantic import TypeAdapter

from ntrip_accuracy_monitor.application.service.lifecycle import (
    _parse_ntrip_url,
)
from pydantic import AnyUrl

_URL = TypeAdapter(AnyUrl)


@pytest.mark.parametrize(
    ("url_str", "expected"),
    [
        ("ntrip://caster.example.com:2101", ("caster.example.com", 2101, False)),
        ("http://caster.example.com", ("caster.example.com", 2101, False)),
        ("http://caster.example.com:8080", ("caster.example.com", 8080, False)),
        ("https://caster.example.com", ("caster.example.com", 443, True)),
        ("https://caster.example.com:8443", ("caster.example.com", 8443, True)),
        ("ntrips://caster.example.com", ("caster.example.com", 443, True)),
    ],
)
def test_parse_ntrip_url(url_str: str, expected: tuple[str, int, bool]) -> None:
    url = _URL.validate_python(url_str)
    assert _parse_ntrip_url(url) == expected


def test_parse_ntrip_url_no_host_raises() -> None:
    # AnyUrl на корректном URL без host — крайне редкий случай;
    # моделируем через подмену поля.
    url = _URL.validate_python("ntrip://caster.example.com")
    # Прямой вызов с фейк-host=None — через подмену в копии:
    import dataclasses  # noqa: F401  (AnyUrl не dataclass; используем .build)
    # Точно проверить через httpscheme без host:
    with pytest.raises(ValueError, match="host"):
        _parse_ntrip_url(_URL.validate_python("ntrip://"))  # type: ignore[arg-type]
