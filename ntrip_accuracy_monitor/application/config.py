"""Типизированная конфигурация приложения.

Источник: TOML-файл через stdlib tomllib + переменные окружения для
чувствительных значений. Все объекты — pydantic v2 модели; валидация
выполняется на этапе model_validate().

Чувствительные поля НИКОГДА не читаются из TOML:
  — PG_PASSWORD (обязательная) → postgres.password.
  — NTRIP_UPSTREAM_PASSWORD (опциональная) → upstream.password.
Если пароль случайно оказался в TOML, он молча отбрасывается и
замещается значением из env (либо отсутствует).

Проектное решение: Подмешивание env происходит в load_config() — единственное
место в проекте, где читается os.environ.
"""

from __future__ import annotations

import os
import tomllib
from pathlib import Path
from typing import Literal, Self

from pydantic import (
    AnyUrl,
    BaseModel,
    Field,
    SecretStr,
    field_validator,
    model_validator,
)

from ntrip_accuracy_monitor.protocols.backoff import BackoffPolicy

_PG_PASSWORD_ENV: str = "PG_PASSWORD"
_NTRIP_UPSTREAM_PASSWORD_ENV: str = "NTRIP_UPSTREAM_PASSWORD"

_EFT_RS3_DEFAULT_NMEA_PORT: int = 9001
"""Штатный TCP-порт NMEA на EFT RS3; используется как default в StreamConfig."""


class PostgresConfig(BaseModel):
    """Параметры подключения к PostgreSQL и размер пула asyncpg."""

    host: str = "localhost"
    port: int = Field(default=5432, ge=1, le=65535)
    database: str
    user: str
    password: SecretStr
    min_pool_size: int = Field(default=2, ge=1)
    max_pool_size: int = Field(default=10, ge=1)

    @model_validator(mode="after")
    def pool_sizes_consistent(self) -> Self:
        if self.max_pool_size < self.min_pool_size:
            raise ValueError(
                "max_pool_size must be >= min_pool_size "
                f"(got max={self.max_pool_size}, min={self.min_pool_size})"
            )
        return self


class BackoffConfig(BaseModel):
    """TOML-friendly mirror of BackoffPolicy.

    Translated to the runtime BackoffPolicy via to_policy() at app
    startup. Defaults follow RTCM 10410.1 §5 implementation note about
    increasing wait times between retries (1s → cap 60s, factor 2).
    """

    initial_delay_s: float = Field(default=1.0, gt=0.0)
    max_delay_s: float = Field(default=60.0, gt=0.0)
    multiplier: float = Field(default=2.0, gt=1.0)
    jitter: float = Field(default=0.1, ge=0.0)

    @model_validator(mode="after")
    def max_ge_initial(self) -> Self:
        if self.max_delay_s < self.initial_delay_s:
            raise ValueError(
                f"max_delay_s ({self.max_delay_s}) must be >= "
                f"initial_delay_s ({self.initial_delay_s})"
            )
        return self

    def to_policy(self) -> BackoffPolicy:
        return BackoffPolicy(
            initial_delay_s=self.initial_delay_s,
            max_delay_s=self.max_delay_s,
            multiplier=self.multiplier,
            jitter=self.jitter,
        )


class NtripCasterConfig(BaseModel):
    """Параметры собственного NTRIP-кастера, отдающего RTCM роверу."""

    host: str = "0.0.0.0"
    port: int = Field(default=2101, ge=1, le=65535)
    mountpoint: str


class NtripUpstreamConfig(BaseModel):
    """Апстрим NTRIP-источник (внешний кастер), если используется.

    Поле `password` принимается только через переменную окружения
    NTRIP_UPSTREAM_PASSWORD (см. load_config). Наличие пароля в TOML
    игнорируется.
    """

    enabled: bool = False
    url: AnyUrl | None = None
    mountpoint: str | None = None
    user: str | None = None
    password: SecretStr | None = None

    ntrip_version: Literal["1.0", "2.0"] = "2.0"
    connect_timeout_s: float = Field(default=10.0, gt=0.0)
    stall_timeout_s: float = Field(default=15.0, gt=0.0)
    queue_max_size: int = Field(default=1024, ge=16)
    backoff: BackoffConfig = BackoffConfig()

    @model_validator(mode="after")
    def required_when_enabled(self) -> Self:
        if self.enabled and (self.url is None or self.mountpoint is None):
            raise ValueError(
                "upstream.enabled=true requires both 'url' and 'mountpoint'"
            )
        return self


class StreamConfig(BaseModel):
    """Один NMEA-источник (приёмник EFT RS3)."""

    stream_id: str
    host: str
    port: int = Field(default=_EFT_RS3_DEFAULT_NMEA_PORT, ge=1, le=65535)
    role: Literal["base", "rover_rtk", "rover_spp"]

    @field_validator("stream_id")
    @classmethod
    def stream_id_non_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("stream_id must be a non-empty string")
        return v


class ReferenceConfig(BaseModel):
    """Эталонные координаты общей антенны (из RTKLIB пост-обработки)."""

    latitude_deg: float = Field(ge=-90.0, le=90.0)
    longitude_deg: float = Field(ge=-180.0, le=180.0)
    ellipsoidal_height_m: float = Field(ge=-1000.0, le=20000.0)
    source: str = "RTKLIB rtkpost"


class AppConfig(BaseModel):
    """Корневая модель конфигурации приложения."""

    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    postgres: PostgresConfig
    caster: NtripCasterConfig
    upstream: NtripUpstreamConfig = NtripUpstreamConfig()
    streams: list[StreamConfig] = Field(min_length=1)
    reference: ReferenceConfig

    @field_validator("streams")
    @classmethod
    def stream_ids_unique(cls, v: list[StreamConfig]) -> list[StreamConfig]:
        ids = [s.stream_id for s in v]
        if len(ids) != len(set(ids)):
            duplicates = sorted({x for x in ids if ids.count(x) > 1})
            raise ValueError(f"stream_id values must be unique; duplicates: {duplicates}")
        return v


def load_config(path: Path) -> AppConfig:
    """Загрузить TOML, подмешать пароли из env, провалидировать.

    Env-переменные:
        PG_PASSWORD: обязательная, пароль PostgreSQL.
        NTRIP_UPSTREAM_PASSWORD: опциональная, пароль для upstream NTRIP.

    Args:
        path: путь к config.toml.

    Returns:
        Провалидированный AppConfig.

    Raises:
        ValueError: если PG_PASSWORD не задана в env, либо если секция
            postgres/upstream в TOML не является таблицей.
        FileNotFoundError: если файл не существует.
        tomllib.TOMLDecodeError: если TOML синтаксически некорректен.
        pydantic.ValidationError: при нарушении схемы.
    """
    with path.open("rb") as f:
        raw: dict[str, object] = tomllib.load(f)

    # --- PG_PASSWORD: обязательна, только из env ---
    pg_password = os.environ.get(_PG_PASSWORD_ENV)
    if pg_password is None:
        raise ValueError(f"{_PG_PASSWORD_ENV} env var is required")

    postgres_section = raw.setdefault("postgres", {})
    if not isinstance(postgres_section, dict):
        raise ValueError("'postgres' section must be a TOML table")
    postgres_section.pop("password", None)  # не доверяем TOML для секретов
    postgres_section["password"] = pg_password

    # --- NTRIP_UPSTREAM_PASSWORD: опциональна, только из env ---
    upstream_section = raw.get("upstream")
    if upstream_section is not None and not isinstance(upstream_section, dict):
        raise ValueError("'upstream' section must be a TOML table")
    if isinstance(upstream_section, dict):
        upstream_section.pop("password", None)  # пароль в TOML игнорируется
        ntrip_password = os.environ.get(_NTRIP_UPSTREAM_PASSWORD_ENV)
        if ntrip_password is not None:
            upstream_section["password"] = ntrip_password

    return AppConfig.model_validate(raw)
