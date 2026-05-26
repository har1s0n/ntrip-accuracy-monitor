"""Типизированная конфигурация приложения.

Источник: TOML-файл через stdlib tomllib + переменные окружения для
чувствительных значений. Все объекты — pydantic v2 модели; валидация
выполняется на этапе model_validate().

Чувствительные поля НИКОГДА не читаются из TOML:
  — PG_PASSWORD                  (обязательная) → postgres.password.
  — UPSTREAM_NTRIP_PASSWORD       (опциональная) → upstream_ntrip.password.
Если пароль случайно оказался в TOML, он молча отбрасывается и
замещается значением из env (либо отсутствует).

Терминология:
  local_caster      — наш собственный NtripCasterServer (раздаёт RTCM роверам);
  upstream_ntrip    — внешний NTRIP-источник RTCM (RS3 #1, BKG, IGS, EUREF-IP);
  nmea_receivers    — TCP-источники NMEA от приёмников EFT RS3.
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
_UPSTREAM_NTRIP_PASSWORD_ENV: str = "UPSTREAM_NTRIP_PASSWORD"
_LOCAL_CASTER_PASSWORD_ENV: str = "LOCAL_CASTER_PASSWORD"

_EFT_RS3_DEFAULT_NMEA_PORT: int = 9001
"""Штатный TCP-порт NMEA на EFT RS3."""


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
    """TOML-friendly mirror of BackoffPolicy."""

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


class LocalCasterConfig(BaseModel):
    """Параметры собственного NTRIP-кастера (наш NtripCasterServer).

    Раздаёт RTCM роверам RS3 #2/#3, которые подключаются к нему
    как Ntrip-клиенты.
    """

    host: str = "0.0.0.0"
    port: int = Field(default=2101, ge=1, le=65535)
    mountpoint: str

    # Basic auth. Если оба None — кастер отдаёт mountpoint без аутентификации.
    username: str | None = None
    password: SecretStr | None = None

    sourcetable_country: str = Field(default="POL", min_length=3, max_length=3)
    subscriber_queue_size: int = Field(default=256, ge=1)
    handshake_timeout_s: float = Field(default=10.0, gt=0)


class UpstreamNtripConfig(BaseModel):
    """Внешний NTRIP-источник RTCM (RS3 #1 как Ntrip-Caster, BKG, IGS, EUREF-IP).

    К нему подключается наш NtripClient. Поле `password` принимается
    только через переменную окружения UPSTREAM_NTRIP_PASSWORD.
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

    gga_source_receiver_id: str | None = None
    """receiver_id из nmea_receivers, чья GGA уходит в кастер.
    None — GGA-uplink выключен (для не-VRS кастеров без GGA-switching).
    Указанный receiver должен иметь role 'rover_rtk' или 'rover_spp'
    (cross-field валидация в AppConfig)."""

    gga_interval_s: float = Field(default=10.0, gt=0.0)
    """Период отправки GGA вверх по NTRIP-соединению. Передаётся в
    NtripClient.gga_interval_s. Стандарт VRS-кастеров — 10-30 секунд."""

    @model_validator(mode="after")
    def required_when_enabled(self) -> Self:
        if self.enabled and (self.url is None or self.mountpoint is None):
            raise ValueError(
                "upstream_ntrip.enabled=true requires both 'url' and 'mountpoint'"
            )
        return self


class CapturesConfig(BaseModel):
    """Параметры FileRtcmSink — записи сырого RTCM-потока в файлы.

    При enabled=True для каждого сеанса создаётся файл
    {directory}/{session_id:06d}_{stream_id}.bin, куда уходят все
    RTCM-фреймы из RtcmHub без интерпретации. Файл перезаписывается
    при повторном открытии того же session_id (на практике
    невозможно — session_id выдаёт БД).
    """

    enabled: bool = False
    directory: Path = Path("./captures")


class NmeaReceiverConfig(BaseModel):
    """Один TCP-источник NMEA — приёмник EFT RS3."""

    receiver_id: str
    host: str
    port: int = Field(default=_EFT_RS3_DEFAULT_NMEA_PORT, ge=1, le=65535)
    role: Literal["base", "rover_rtk", "rover_spp"]

    @field_validator("receiver_id")
    @classmethod
    def receiver_id_non_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("receiver_id must be a non-empty string")
        return v


class ReferenceAntennaConfig(BaseModel):
    """Эталонные координаты общей антенны №2 (из RTKLIB пост-обработки).

    latitude_deg/longitude_deg/ellipsoidal_height_m — центр локальной
    системы ENU при расчёте метрик точности. Обязательные.

    sigma_*_mm — empirical σ эталона по N/E/U (миллиметры) из пост-обработки RTKLIB.
    """

    latitude_deg: float = Field(ge=-90.0, le=90.0)
    longitude_deg: float = Field(ge=-180.0, le=180.0)
    ellipsoidal_height_m: float = Field(ge=-1000.0, le=20000.0)

    sigma_north_mm: float | None = Field(default=None, gt=0.0)
    sigma_east_mm: float | None = Field(default=None, gt=0.0)
    sigma_up_mm: float | None = Field(default=None, gt=0.0)

    source: str = "RTKLIB rtkpost"


class AppConfig(BaseModel):
    """Корневая модель конфигурации приложения."""

    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    postgres: PostgresConfig
    local_caster: LocalCasterConfig
    upstream_ntrip: UpstreamNtripConfig = UpstreamNtripConfig()
    nmea_receivers: list[NmeaReceiverConfig] = Field(min_length=1)
    reference_antenna: ReferenceAntennaConfig
    captures: CapturesConfig = CapturesConfig()

    @field_validator("nmea_receivers")
    @classmethod
    def receiver_ids_unique(
        cls, v: list[NmeaReceiverConfig],
    ) -> list[NmeaReceiverConfig]:
        ids = [r.receiver_id for r in v]
        if len(ids) != len(set(ids)):
            duplicates = sorted({x for x in ids if ids.count(x) > 1})
            raise ValueError(
                f"receiver_id values must be unique; duplicates: {duplicates}"
            )
        return v

    @model_validator(mode="after")
    def gga_source_receiver_resolvable(self) -> Self:
        """Если задан upstream_ntrip.gga_source_receiver_id, такой
        receiver должен существовать в nmea_receivers и иметь роль
        ровера (отправка GGA от базы в VRS-кастер бессмысленна).

        Не проверяется при upstream_ntrip.enabled=False — позволяет
        временно выключить uplink без удаления настроек GGA-источника.
        """
        if not self.upstream_ntrip.enabled:
            return self
        src_id = self.upstream_ntrip.gga_source_receiver_id
        if src_id is None:
            return self
        matching = [r for r in self.nmea_receivers if r.receiver_id == src_id]
        if not matching:
            raise ValueError(
                f"upstream_ntrip.gga_source_receiver_id={src_id!r} not found "
                f"in nmea_receivers; available: "
                f"{[r.receiver_id for r in self.nmea_receivers]}"
            )
        role = matching[0].role
        if role not in ("rover_rtk", "rover_spp"):
            raise ValueError(
                f"upstream_ntrip.gga_source_receiver_id={src_id!r} has role "
                f"{role!r}; must be 'rover_rtk' or 'rover_spp' (sending GGA "
                f"from a base station to a VRS caster is meaningless)"
            )
        return self


def load_config(path: Path) -> AppConfig:
    """Загрузить TOML, подмешать пароли из env, провалидировать.

    Env-переменные:
        PG_PASSWORD: обязательная.
        UPSTREAM_NTRIP_PASSWORD: опциональная.

    Raises:
        ValueError: если PG_PASSWORD не задана либо секции некорректны.
        FileNotFoundError, tomllib.TOMLDecodeError, pydantic.ValidationError.
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
    postgres_section.pop("password", None)
    postgres_section["password"] = pg_password

    caster_section = raw.get("local_caster")
    if caster_section is not None and not isinstance(caster_section, dict):
        raise ValueError("'local_caster' section must be a TOML table")
    if isinstance(caster_section, dict):
        caster_section.pop("password", None)
        caster_password = os.environ.get(_LOCAL_CASTER_PASSWORD_ENV)
        if caster_password is not None:
            caster_section["password"] = caster_password

    # --- UPSTREAM_NTRIP_PASSWORD: опциональна, только из env ---
    upstream_section = raw.get("upstream_ntrip")
    if upstream_section is not None and not isinstance(upstream_section, dict):
        raise ValueError("'upstream_ntrip' section must be a TOML table")
    if isinstance(upstream_section, dict):
        upstream_section.pop("password", None)
        ntrip_password = os.environ.get(_UPSTREAM_NTRIP_PASSWORD_ENV)
        if ntrip_password is not None:
            upstream_section["password"] = ntrip_password

    return AppConfig.model_validate(raw)
