"""Парсер RTCM3-фрейма в типизированную метаданную (RtcmMessage).

Вход:  bytes (полный фрейм с D3-преамбулой и валидным CRC-24Q —
       валидация выполнена в protocols/ntrip/_framer.py).
Выход: RtcmMessage с message_type / station_id / epoch_time_ms.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final

from pyrtcm import RTCMMessage, RTCMReader
from pyrtcm.exceptions import (
    RTCMMessageError,
    RTCMParseError,
    RTCMTypeError,
)

from ntrip_accuracy_monitor.protocols.ntrip._framer import extract_msg_type


class RtcmParseError(Exception):
    """Не удалось разобрать RTCM-фрейм даже на уровне типа сообщения."""


# Сообщения наблюдений по системам — у них есть поле эпохи.
# RTCM 10403.x:
#   GPS  legacy 1001-1004 → DF004 (GPS Epoch Time, ms от начала GPS-недели)
#   GPS  MSM    1071-1077 → DF004
#   GLO  legacy 1009-1012 → DF034 (GLONASS Epoch Time, ms от начала дня MSK)
_GPS_LEGACY_OBS: Final[frozenset[int]] = frozenset({1001, 1002, 1003, 1004})
_GLO_LEGACY_OBS: Final[frozenset[int]] = frozenset({1009, 1010, 1011, 1012})
_GPS_MSM: Final[frozenset[int]] = frozenset({1071, 1072, 1073, 1074, 1075, 1076, 1077})


@dataclass(frozen=True, slots=True)
class RtcmMessage:
    """Типизированная метаданная одного RTCM3-фрейма."""

    raw: bytes
    """Полный фрейм с D3-преамбулой и CRC-24Q. Передаётся в БД as-is."""

    message_type: int
    """DF002, 12 бит. Гарантированно проставлено."""

    received_at: datetime
    """Момент попадания фрейма в адаптер (UTC, timezone-aware)."""

    station_id: int | None = None
    """DF003, для типов с reference station ID (1001-1013, 1019, 1020, 1033, MSM)."""

    epoch_time_ms: int | None = None
    """Поле эпохи для observation messages. None для эфемерид и station info."""


class RtcmAdapter:
    """bytes → RtcmMessage. Eager-парсинг. Stateless, многократный."""

    def parse(self, raw: bytes) -> RtcmMessage:
        """Распарсить один CRC-валидный RTCM3-фрейм.

        Raises:
            RtcmParseError: если pyrtcm упал И message_type не извлекается
                            из сырых байтов (то есть фрейм радикально битый).
        """
        received_at = datetime.now(UTC)

        # Fallback message_type из сырых байт через общий хелпер _framer'а.
        # Нужен на случай, если pyrtcm упадёт на неизвестном/новом типе
        # сообщения. NTRIP-monitor не должен слепнуть из-за нового RTCM-типа.
        raw_type = _message_type_from_frame(raw)

        try:
            parsed = RTCMReader.parse(raw)
        except (RTCMMessageError, RTCMParseError, RTCMTypeError) as exc:
            if raw_type is None:
                raise RtcmParseError(
                    f"pyrtcm failed and message type cannot be recovered: {exc}"
                ) from exc
            return RtcmMessage(
                raw=raw,
                message_type=raw_type,
                received_at=received_at,
            )

        try:
            mtype = int(parsed.identity)
        except (AttributeError, ValueError) as exc:
            if raw_type is None:
                raise RtcmParseError(
                    f"pyrtcm produced no usable identity: {exc}"
                ) from exc
            mtype = raw_type

        station_id_raw = getattr(parsed, "DF003", None)
        station_id = int(station_id_raw) if station_id_raw is not None else None

        epoch_time_ms = _extract_epoch_ms(parsed, mtype)

        return RtcmMessage(
            raw=raw,
            message_type=mtype,
            received_at=received_at,
            station_id=station_id,
            epoch_time_ms=epoch_time_ms,
        )


def _message_type_from_frame(raw: bytes) -> int | None:
    """Извлечь DF002 из полного RTCM3-фрейма через общий хелпер.

    Layout (RTCM 10403.x §4):
      raw[0]    = 0xD3 preamble
      raw[1:3]  = 6 reserved + 10-bit length
      raw[3:]   = payload (DF002 — первые 12 бит)
    """
    if len(raw) < 5 or raw[0] != 0xD3:
        return None
    mtype = extract_msg_type(raw[3:])
    return mtype if mtype != 0 else None


def _extract_epoch_ms(parsed: RTCMMessage, msg_type: int) -> int | None:
    """Достать поле эпохи в зависимости от семейства сообщений."""
    if msg_type in _GPS_LEGACY_OBS or msg_type in _GPS_MSM:
        v = getattr(parsed, "DF004", None)
        return int(v) if v is not None else None
    if msg_type in _GLO_LEGACY_OBS:
        v = getattr(parsed, "DF034", None)
        return int(v) if v is not None else None
    return None
