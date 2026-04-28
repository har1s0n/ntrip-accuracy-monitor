"""Reconnect-политика

Инфраструктурный примитив сетевых протоколов (NMEA-транспорт и NTRIP-клиент).
"""

from __future__ import annotations

import random
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class BackoffPolicy:
    """Параметры Reconnect-политики

    Задержка перед попыткой `attempt` (0-индексация: 0 = первая retry-попытка
    после неудачного коннекта) вычисляется как
    `min(initial_delay_s * multiplier**attempt, max_delay_s)`,
    после чего к ней добавляется случайный сдвиг в диапазоне
    `±jitter * base`.

    Все поля иммутабельны: политика — value-object, безопасно шарить между
    транспортами.
    """

    initial_delay_s: float
    max_delay_s: float
    multiplier: float = 2.0
    jitter: float = 0.1  # доля от base, [0.0; 1.0]

    def __post_init__(self) -> None:
        if self.initial_delay_s <= 0.0:
            raise ValueError("initial_delay_s must be > 0")
        if self.max_delay_s < self.initial_delay_s:
            raise ValueError("max_delay_s must be >= initial_delay_s")
        if self.multiplier < 1.0:
            raise ValueError("multiplier must be >= 1.0")
        if not 0.0 <= self.jitter <= 1.0:
            raise ValueError("jitter must be in [0.0, 1.0]")

    def delay_for_attempt(
        self,
        attempt: int,
        *,
        rng: random.Random | None = None,
    ) -> float:
        """Вернуть задержку (с) перед `attempt`-й retry-попыткой."""
        if attempt < 0:
            raise ValueError("attempt must be >= 0")
        base = min(
            self.initial_delay_s * (self.multiplier ** attempt),
            self.max_delay_s,
        )
        if self.jitter == 0.0:
            return base
        gen = rng if rng is not None else random.SystemRandom()
        spread = base * self.jitter
        return max(0.0, base + gen.uniform(-spread, spread))
