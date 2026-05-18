"""Сшивание NMEA-сообщений одного канала в типизированный Epoch.

Эпоха ограничена двумя соседними GGA: GGA n — якорь, все сообщения
до следующего GGA n+1 ассоциируются с GGA n. На приходе GGA n+1
эпоха закрывается и отдаётся через on_epoch.

Дата для epoch_time:
    приоритет ZDA → RMC(valid) → fallback (системная UTC-дата при первом GGA)
    + перенос на сутки при наблюдаемом регрессе времени GGA (23:59:5x → 00:00:0x).

Дата из самого GGA.time_utc игнорируется
Используем только GGA.time_utc.time() (HH:MM:SS).
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, date, datetime, time, timedelta
from typing import Final

from ntrip_accuracy_monitor.domain.epoch import Epoch
from ntrip_accuracy_monitor.protocols.nmea.messages import (
    GgaRecord,
    GsaRecord,
    GstRecord,
    NmeaRecord,
    RmcRecord,
    ZdaRecord,
)

_LOG: Final = logging.getLogger(__name__)

# Приоритет источников даты: больше — надёжнее.
_DATE_PRIORITY: Final[dict[str, int]] = {
    "none": 0,
    "fallback": 1,
    "rmc": 2,
    "zda": 3,
}


def _positive_or_none(value: float | None) -> float | None:
    """Привести к None всё, что не строго положительно.

    Epoch валидирует hdop/pdop/sigma_* как > 0; приёмники иногда
    шлют 0.0 при отсутствующей оценке. Нормализуем такие к None,
    чтобы не падать на конструкции Epoch.
    """
    if value is None or value <= 0.0:
        return None
    return value


class EpochAggregator:
    """Сшивает поток NMEA-сообщений одного канала в Epoch.

    Не делает I/O. Выдача эпох — через переданный async-обработчик
    on_epoch. Все счётчики отбрасываний — публичные атрибуты для
    тестов и логирования.
    """

    def __init__(
        self,
        stream_id: str,
        on_epoch: Callable[[Epoch], Awaitable[None]],
    ) -> None:
        self._stream_id = stream_id
        self._on_epoch = on_epoch

        # Состояние текущей открытой эпохи.
        self._current_gga: GgaRecord | None = None
        self._current_gst: GstRecord | None = None
        self._current_gsa: GsaRecord | None = None

        # Отслеживание даты.
        self._current_date: date | None = None
        self._date_source: str = "none"
        self._fallback_warned: bool = False

        # Для определения перехода через полночь — последнее время GGA,
        # которое уже было эмитировано (без даты, чистое HH:MM:SS).
        self._last_emitted_gga_time: time | None = None

        # Публичные счётчики.
        self.dropped_no_position: int = 0
        self.dropped_invalid_format: int = 0

    @property
    def stream_id(self) -> str:
        return self._stream_id

    @property
    def current_date(self) -> date | None:
        """Текущая дата агрегатора (для тестов и диагностики)."""
        return self._current_date

    @property
    def date_source(self) -> str:
        """Источник текущей даты: zda | rmc | fallback | none."""
        return self._date_source

    async def consume(self, message: NmeaRecord) -> None:
        """Подать одно типизированное NMEA-сообщение."""
        match message:
            case GgaRecord():
                await self._handle_gga(message)
            case GstRecord():
                # Принадлежит ещё не закрытой эпохе (предыдущему GGA).
                if self._current_gga is not None:
                    self._current_gst = message
            case GsaRecord():
                if self._current_gga is not None:
                    self._current_gsa = message
            case RmcRecord():
                if message.is_valid:
                    self._adopt_date(message.time_utc.date(), "rmc")
            case ZdaRecord():
                self._adopt_date(message.time_utc.date(), "zda")
            case _:
                # Незнакомый тип — игнорируем.
                pass

    async def flush_pending(self) -> None:
        """Принудительно выдать текущую открытую эпоху (на остановке)."""
        if self._current_gga is not None:
            await self._emit_current_epoch()
            self._reset_current()

    async def _handle_gga(self, gga: GgaRecord) -> None:
        if self._current_gga is not None:
            await self._emit_current_epoch()
            self._reset_current()
        self._current_gga = gga

    def _reset_current(self) -> None:
        self._current_gga = None
        self._current_gst = None
        self._current_gsa = None

    async def _emit_current_epoch(self) -> None:
        gga = self._current_gga
        assert gga is not None

        # Критерий отбрасывания — отсутствие позиции. По контракту GgaRecord
        # это случается только при solution_mode == INVALID без фикса.
        if gga.position is None:
            self.dropped_no_position += 1
            if self.dropped_no_position == 1:
                _LOG.warning(
                    "stream=%s: пропуск GGA без позиции (счётчик растёт)",
                    self._stream_id,
                )
            return

        gga_time = gga.time_utc.time()
        epoch_date = self._resolve_date_for_gga(gga_time)
        epoch_dt = datetime.combine(epoch_date, gga_time, tzinfo=UTC)

        # DOP и σ нормализуем: <= 0 трактуем как «нет оценки».
        hdop = _positive_or_none(
            self._current_gsa.hdop
            if self._current_gsa is not None
               and self._current_gsa.hdop is not None
            else gga.hdop
        )
        pdop = _positive_or_none(
            self._current_gsa.pdop if self._current_gsa is not None else None
        )

        sigma_e: float | None = None
        sigma_n: float | None = None
        sigma_u: float | None = None
        if self._current_gst is not None:
            sigma_e = _positive_or_none(self._current_gst.sigma_east_m)
            sigma_n = _positive_or_none(self._current_gst.sigma_north_m)
            sigma_u = _positive_or_none(self._current_gst.sigma_up_m)

        try:
            epoch = Epoch(
                epoch_time=epoch_dt,
                stream_id=self._stream_id,
                position=gga.position,
                solution_mode=gga.solution_mode,
                age_of_corrections_s=gga.age_of_corrections_s,
                satellites_used=gga.satellites_used,
                hdop=hdop,
                pdop=pdop,
                sigma_east_m=sigma_e,
                sigma_north_m=sigma_n,
                sigma_up_m=sigma_u,
            )
        except ValueError as exc:
            # Защита от полей вне диапазона Epoch.__post_init__:
            # age > 3600 c, отрицательное satellites_used и т.п.
            self.dropped_invalid_format += 1
            if self.dropped_invalid_format == 1:
                _LOG.warning(
                    "stream=%s: эпоха отброшена валидацией Epoch: %s",
                    self._stream_id,
                    exc,
                )
            return

        await self._on_epoch(epoch)
        self._last_emitted_gga_time = gga_time

    def _resolve_date_for_gga(self, gga_time: time) -> date:
        """Вернуть дату, к которой относится GGA с указанным временем.

        Никогда не возвращает None: при отсутствии источников даты
        используется системная UTC-дата (с предупреждением — один раз).
        """
        if self._current_date is None:
            today = datetime.now(UTC).date()
            self._current_date = today
            self._date_source = "fallback"
            if not self._fallback_warned:
                _LOG.warning(
                    "stream=%s: нет источника даты (ZDA/RMC), "
                    "используем системную UTC %s — режим деградирован",
                    self._stream_id,
                    today,
                )
                self._fallback_warned = True

        # Перенос через полночь: фиксируем регресс времени между
        # предыдущей выданной эпохой и текущей.
        if (
            self._last_emitted_gga_time is not None
            and gga_time < self._last_emitted_gga_time
        ):
            previous = self._current_date
            self._current_date = self._current_date + timedelta(days=1)
            _LOG.info(
                "stream=%s: GGA-время пошло назад (%s → %s), "
                "перенос даты %s → %s",
                self._stream_id,
                self._last_emitted_gga_time,
                gga_time,
                previous,
                self._current_date,
            )
        return self._current_date

    def _adopt_date(self, new_date: date, source: str) -> None:
        """Применить дату от ZDA/RMC, если её источник не ниже текущего."""
        incoming = _DATE_PRIORITY[source]
        active = _DATE_PRIORITY[self._date_source]
        if incoming < active:
            return
        if self._current_date != new_date:
            _LOG.info(
                "stream=%s: смена даты %s (%s) → %s (%s)",
                self._stream_id,
                self._current_date,
                self._date_source,
                new_date,
                source,
            )
        self._current_date = new_date
        self._date_source = source
