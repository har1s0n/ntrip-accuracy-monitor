"""Центральная доменная сущность — одна эпоха измерений (1 Гц = 1 запись).

Эпоха содержит сшитые данные одного канала (rover_rtk / rover_spp / base)
на момент времени epoch_time: позицию, режим, возраст поправок, геометрию
созвездия и — если доступны — 1σ оценки ошибок из GST.

Поля с None означают "данные этого типа в данной эпохе отсутствуют"
(например, age_of_corrections для SPP; pdop для канала без $GxGSA;
sigma_* — при отсутствии $GxGST).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from ntrip_accuracy_monitor.domain.position import GeodeticPosition
from ntrip_accuracy_monitor.domain.solution_mode import SolutionMode
from ntrip_accuracy_monitor.domain.time_scales import ensure_utc

"""Час и более — практически неработоспособные поправки; флагируем как ошибку."""
MAX_PLAUSIBLE_AGE_OF_CORRECTIONS_S: float = 3600.0


@dataclass(frozen=True, slots=True)
class Epoch:
    """Доменная запись одной эпохи измерений (неизменяемая)."""

    epoch_time: datetime
    """Момент измерения; TZ-aware UTC (валидируется через ensure_utc)."""

    stream_id: str
    """Идентификатор канала: 'rover_rtk' | 'rover_spp' | 'base' | ..."""

    position: GeodeticPosition

    solution_mode: SolutionMode

    age_of_corrections_s: float | None
    """GGA поле 13. None для SPP и для baseline-канала без поправок."""

    satellites_used: int
    """GGA поле 7."""

    hdop: float | None
    """GGA поле 8. Может отсутствовать."""

    pdop: float | None
    """Из GSA; может отсутствовать (GSA не передаётся)."""

    sigma_east_m: float | None
    """1σ оценка из GST, метры."""

    sigma_north_m: float | None
    """1σ оценка из GST, метры."""

    sigma_up_m: float | None
    """1σ оценка из GST, метры."""

    def __post_init__(self) -> None:
        # epoch_time: TZ-aware UTC. Замораживаем проверенное значение
        # через object.__setattr__ — обязательно для frozen=True.
        validated_time = ensure_utc(self.epoch_time)
        if validated_time is not self.epoch_time:
            object.__setattr__(self, "epoch_time", validated_time)

        if not self.stream_id:
            raise ValueError("stream_id must be a non-empty string")

        if self.satellites_used < 0:
            raise ValueError(
                f"satellites_used must be >= 0, got {self.satellites_used!r}"
            )

        if self.age_of_corrections_s is not None:
            if self.age_of_corrections_s < 0.0:
                raise ValueError(
                    "age_of_corrections_s must be >= 0, "
                    f"got {self.age_of_corrections_s!r}"
                )
            if self.age_of_corrections_s > MAX_PLAUSIBLE_AGE_OF_CORRECTIONS_S:
                raise ValueError(
                    "age_of_corrections_s exceeds plausible range "
                    f"(>{MAX_PLAUSIBLE_AGE_OF_CORRECTIONS_S} s): "
                    f"{self.age_of_corrections_s!r}"
                )

        for name in ("hdop", "pdop", "sigma_east_m", "sigma_north_m", "sigma_up_m"):
            value: float | None = getattr(self, name)
            if value is not None and value <= 0.0:
                raise ValueError(f"{name} must be > 0 when provided, got {value!r}")
