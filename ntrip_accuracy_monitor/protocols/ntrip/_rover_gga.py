"""GGA-provider, источник — поток NMEA от ровера.

Кэширует последний GgaRecord с непустой позицией. По вызову provide()
регенерирует GGA-сообщение через encode_static_gga со свежим UTC и
актуальной позицией из кэша. Контракт provide() совместим с
NtripClient.gga_provider.

Используется как замена static_gga_provider для VRS-style и GGA-switching
кастеров (включая EFT RS3 во встроенном Ntrip-кастере), когда живой поток
NMEA от ровера уже доступен.

Свойства, важные для оператора:
  - GGA с solution_mode == INVALID (position is None) кэш не обновляет —
    при кратковременной потере фикса в кастер уходит последняя валидная
    позиция, ровер далеко уехать не успел.
  - До первой валидной GGA provide() возвращает None — NtripClient
    штатно пропускает тик GGA-uplink на None.
  - Подписка на конкретный канал — забота SessionLifecycle: провайдер
    к receiver_id не привязан, фильтрация делается тем, что
    его consume() подключают только к одному NmeaTcpClient.
"""

from __future__ import annotations

import logging
from typing import Final

from ntrip_accuracy_monitor.protocols.nmea.messages import GgaRecord, NmeaRecord
from ntrip_accuracy_monitor.protocols.ntrip._gga import encode_static_gga

logger: Final = logging.getLogger(__name__)

_DEFAULT_HDOP: Final[float] = 1.0


class RoverGgaProvider:
    """Кэш последней валидной GGA + регенерация сообщения по запросу."""

    def __init__(self) -> None:
        self._latest: GgaRecord | None = None
        self._consumed_gga_total: int = 0
        self._consumed_gga_with_fix: int = 0
        self._provided_total: int = 0
        self._provided_empty: int = 0

    @property
    def has_fix(self) -> bool:
        """True, если хотя бы одна GGA с позицией уже была получена."""
        return self._latest is not None

    @property
    def latest_record(self) -> GgaRecord | None:
        """Последняя валидная GGA. Только для диагностики / тестов."""
        return self._latest

    @property
    def consumed_gga_total(self) -> int:
        return self._consumed_gga_total

    @property
    def consumed_gga_with_fix(self) -> int:
        return self._consumed_gga_with_fix

    @property
    def provided_total(self) -> int:
        return self._provided_total

    @property
    def provided_empty(self) -> int:
        return self._provided_empty

    async def consume(self, record: NmeaRecord) -> None:
        """Подписчик NMEA-потока.

        Все не-GGA сообщения игнорируются. GGA без позиции (INVALID)
        счетчик инкрементирует, но кэш не обновляет — сохраняем последнюю
        валидную позицию на случай кратковременной потери фикса.
        """
        if not isinstance(record, GgaRecord):
            return
        self._consumed_gga_total += 1
        if record.position is None:
            return
        self._consumed_gga_with_fix += 1
        self._latest = record

    async def provide(self) -> bytes | None:
        """Контракт NtripClient.gga_provider.

        Returns:
            None, пока ни одной валидной GGA не было — NtripClient
            пропустит этот тик GGA-uplink (поведение _gga_uplink_loop).
            Иначе — байты свежесгенерированной GGA: координаты из
            последнего фикса, UTC — текущее время (encode_static_gga
            ставит datetime.now(UTC) на каждый вызов).
        """
        self._provided_total += 1
        rec = self._latest
        if rec is None:
            self._provided_empty += 1
            return None
        # consume() гарантирует: position не None для всего, что попадает в _latest.
        assert rec.position is not None
        return encode_static_gga(
            lat_deg=rec.position.latitude_deg,
            lon_deg=rec.position.longitude_deg,
            alt_m=rec.position.ellipsoidal_height_m,
            quality=int(rec.solution_mode),
            sats_used=rec.satellites_used,
            hdop=rec.hdop if rec.hdop is not None else _DEFAULT_HDOP,
        )
