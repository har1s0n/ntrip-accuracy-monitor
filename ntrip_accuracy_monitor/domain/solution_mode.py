"""GGA quality → solution mode mapping.

Источник: NMEA-0183 v4.10, $GxGGA поле 6 (Fix Quality Indicator).
Значения 0..8 жёстко фиксированы стандартом и напрямую используются
в БД как int-колонка. Поэтому IntEnum, а не StrEnum или класс.
"""

from __future__ import annotations

from enum import IntEnum
from typing import Self


class SolutionMode(IntEnum):
    """Режим позиционирования по GGA quality indicator."""

    INVALID = 0
    SPP = 1
    DGNSS = 2
    PPS = 3
    RTK_FIXED = 4
    RTK_FLOAT = 5
    DEAD_RECKONING = 6
    MANUAL = 7
    SIMULATOR = 8

    @classmethod
    def from_gga_quality(cls, quality: int) -> Self:
        """Преобразовать значение поля 6 NMEA GGA в SolutionMode.

        Raises:
            ValueError: если quality вне диапазона 0..8.
        """
        if not 0 <= quality <= 8:
            raise ValueError(
                f"GGA quality must be in range 0..8, got {quality!r}"
            )
        return cls(quality)

    @property
    def is_fixed_solution(self) -> bool:
        """True только для RTK с фиксированной неоднозначностью."""
        return self is SolutionMode.RTK_FIXED

    @property
    def is_differential(self) -> bool:
        """True для любого режима, использующего дифференциальные поправки."""
        return self in (
            SolutionMode.DGNSS,
            SolutionMode.RTK_FIXED,
            SolutionMode.RTK_FLOAT,
        )

    @property
    def is_usable(self) -> bool:
        """True для любого валидного решения (всё, кроме INVALID)."""
        return self is not SolutionMode.INVALID

    @property
    def human_name(self) -> str | None:
        """Читаемое имя для отчётов и логов."""
        match self:
            case SolutionMode.INVALID:
                return "Invalid"
            case SolutionMode.SPP:
                return "SPP"
            case SolutionMode.DGNSS:
                return "DGNSS"
            case SolutionMode.PPS:
                return "PPS"
            case SolutionMode.RTK_FIXED:
                return "RTK Fixed"
            case SolutionMode.RTK_FLOAT:
                return "RTK Float"
            case SolutionMode.DEAD_RECKONING:
                return "Dead Reckoning"
            case SolutionMode.MANUAL:
                return "Manual"
            case SolutionMode.SIMULATOR:
                return "Simulator"
        return None

    def __str__(self) -> str:
        return self.human_name
