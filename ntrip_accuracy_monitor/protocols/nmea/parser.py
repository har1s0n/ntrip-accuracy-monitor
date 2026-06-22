"""Парсинг NMEA-0183 сообщений в доменные представления.

Тонкая обёртка над pynmeagps: библиотека сама валидирует XOR-checksum
и собирает типизированный NMEAMessage; мы перекладываем поля в наши
доменные dataclass-ы и инкапсулируем чужие исключения.
"""

from datetime import UTC, date, datetime, time
from typing import Final

import pynmeagps
from pynmeagps import NMEAMessage, NMEAReader

from ntrip_accuracy_monitor.domain.position import GeodeticPosition
from ntrip_accuracy_monitor.domain.solution_mode import SolutionMode

from .errors import (
    NmeaChecksumError,
    NmeaParseError,
    NmeaUnsupportedTalkerError,
)
from .messages import (
    GgaRecord,
    GsaRecord,
    GstRecord,
    NmeaRecord,
    RmcRecord,
    ZdaRecord,
)

ALLOWED_TALKERS: Final[frozenset[str]] = frozenset(
    {"GP", "GL", "GA", "GB", "GN", "GQ", "GI"}
)
"""GP=GPS, GL=GLONASS, GA=Galileo, GB=BeiDou, GN=combined, GQ=QZSS, GI=NavIC."""

_TARGET_MSG_IDS: Final[frozenset[str]] = frozenset(
    {"GGA", "GST", "GSA", "RMC", "ZDA"}
)


def parse_line(raw: bytes | str, *, today_utc: date) -> NmeaRecord | None:
    """Парсит одну NMEA-строку.

    Args:
        raw: одна строка NMEA, с/без trailing CRLF.
        today_utc: дата для сборки `time_utc` сообщений типа GGA/GST,
            у которых в payload только HH:MM:SS.sss. Передаётся
            явно — функция stateless и не читает системные часы.

    Returns:
        Доменный record для целевых типов (GGA/GST/GSA/RMC/ZDA) с
        разрешённым talker-id, иначе `None` (нерелевантное сообщение:
        GLL, VTG, GSV, чужой talker и т.п.).

    Raises:
        NmeaChecksumError: невалидная XOR-сумма.
        NmeaParseError: не-NMEA вход или поломанная структура.
    """
    raw_bytes = raw.encode("ascii", errors="strict") if isinstance(raw, str) else raw
    raw_bytes = raw_bytes.rstrip(b"\r\n") + b"\r\n"

    try:
        msg = NMEAReader.parse(raw_bytes, validate=pynmeagps.VALCKSUM)
    except pynmeagps.NMEAParseError as exc:
        # pynmeagps использует один тип исключения и для checksum,
        # и для структурных ошибок — различаем по тексту сообщения.
        if "checksum" in str(exc).lower():
            raise NmeaChecksumError(str(exc)) from exc
        raise NmeaParseError(str(exc)) from exc
    except (pynmeagps.NMEAMessageError, pynmeagps.NMEATypeError) as exc:
        raise NmeaParseError(str(exc)) from exc

    if msg is None:
        raise NmeaParseError(f"not a valid NMEA sentence: {raw_bytes!r}")
    if msg.talker not in ALLOWED_TALKERS or msg.msgID not in _TARGET_MSG_IDS:
        return None

    return _dispatch(msg, today_utc)


def _dispatch(msg: NMEAMessage, today_utc: date) -> NmeaRecord | None:
    """Маппит распознанное сообщение на доменный конвертер.

    Предусловие: msg.talker ∈ ALLOWED_TALKERS, msg.msgID ∈ _TARGET_MSG_IDS.
    """
    match msg.msgID:
        case "GGA":
            return nmea_to_gga(msg, today_utc=today_utc)
        case "GST":
            return nmea_to_gst(msg, today_utc=today_utc)
        case "GSA":
            return nmea_to_gsa(msg)
        case "RMC":
            return nmea_to_rmc(msg)
        case "ZDA":
            return nmea_to_zda(msg)
        case _:
            return None


# ---------- direct converters ----------
def nmea_to_gga(msg: NMEAMessage, *, today_utc: date) -> GgaRecord:
    _require_target(msg, "GGA")

    quality_raw = msg.quality
    if not isinstance(quality_raw, int):
        raise NmeaParseError(f"GGA.quality is not int: {quality_raw!r}")
    solution_mode = SolutionMode.from_gga_quality(quality_raw)

    time_utc = _combine_utc_time(msg.time, today_utc, field="GGA.time")

    lat = _opt_float(msg.lat)
    lon = _opt_float(msg.lon)
    # GGA поле 9 (msg.alt) — ОРТОМЕТРИЧЕСКАЯ высота (над геоидом).
    # GGA поле 11 (msg.sep) — geoid separation N = h_эллипс − H_орто.
    # Эллипсоидальная высота h = alt + sep. Без sep одна alt даёт
    # систематический сдвиг на N (~16 м в МО) → ломает VRMS/3D.
    orthometric_alt = _opt_float(msg.alt)
    geoid_sep = _opt_float(msg.sep)
    if orthometric_alt is not None and geoid_sep is not None:
        ellipsoidal_alt: float | None = orthometric_alt + geoid_sep
    else:
        ellipsoidal_alt = None

    position: GeodeticPosition | None
    if lat is not None and lon is not None and ellipsoidal_alt is not None:
        position = GeodeticPosition(
            latitude_deg=lat,
            longitude_deg=lon,
            ellipsoidal_height_m=ellipsoidal_alt,
        )
    elif solution_mode is SolutionMode.INVALID:
        position = None
    else:
        raise NmeaParseError(
            f"GGA.quality={quality_raw} but position/geoid-sep fields are empty"
        )

    num_sv_raw = msg.numSV
    satellites_used = num_sv_raw if isinstance(num_sv_raw, int) else 0

    station_raw = msg.diffStation
    station = station_raw if isinstance(station_raw, int) else None

    return GgaRecord(
        time_utc=time_utc,
        position=position,
        solution_mode=solution_mode,
        satellites_used=satellites_used,
        hdop=_opt_float(msg.HDOP),
        age_of_corrections_s=_opt_float(msg.diffAge),
        reference_station_id=station,
    )


def nmea_to_gst(msg: NMEAMessage, *, today_utc: date) -> GstRecord:
    _require_target(msg, "GST")
    time_utc = _combine_utc_time(msg.time, today_utc, field="GST.time")

    sigma_lat = _opt_float(msg.stdLat)
    sigma_lon = _opt_float(msg.stdLong)
    sigma_alt = _opt_float(msg.stdAlt)
    if sigma_lat is None or sigma_lon is None or sigma_alt is None:
        raise NmeaParseError("GST sigma fields are empty or non-numeric")

    return GstRecord(
        time_utc=time_utc,
        sigma_east_m=sigma_lon,
        sigma_north_m=sigma_lat,
        sigma_up_m=sigma_alt,
    )


def nmea_to_gsa(msg: NMEAMessage) -> GsaRecord:
    _require_target(msg, "GSA")

    nav_mode = msg.navMode
    if not isinstance(nav_mode, int):
        raise NmeaParseError(f"GSA.navMode is not int: {nav_mode!r}")

    prns: list[int] = []
    for i in range(1, 13):
        svid = getattr(msg, f"svid_{i:02d}", None)
        if isinstance(svid, int) and svid > 0:
            prns.append(svid)

    return GsaRecord(
        pdop=_opt_float(msg.PDOP),
        hdop=_opt_float(msg.HDOP),
        vdop=_opt_float(msg.VDOP),
        fix_type=nav_mode,
        satellite_prns=tuple(prns),
    )


def nmea_to_rmc(msg: NMEAMessage) -> RmcRecord:
    _require_target(msg, "RMC")
    msg_date = msg.date
    msg_time = msg.time
    if not isinstance(msg_date, date) or not isinstance(msg_time, time):
        raise NmeaParseError("RMC date/time fields are missing")
    return RmcRecord(
        time_utc=datetime.combine(msg_date, msg_time, tzinfo=UTC),
        is_valid=(msg.status == "A"),
    )


def nmea_to_zda(msg: NMEAMessage) -> ZdaRecord:
    _require_target(msg, "ZDA")
    day, month, year = msg.day, msg.month, msg.year
    msg_time = msg.time
    if (
        not isinstance(day, int)
        or not isinstance(month, int)
        or not isinstance(year, int)
        or not isinstance(msg_time, time)
    ):
        raise NmeaParseError("ZDA date/time fields are missing")
    return ZdaRecord(
        time_utc=datetime.combine(date(year, month, day), msg_time, tzinfo=UTC),
    )


# ---------- helpers ----------
def _require_target(msg: NMEAMessage, expected_msg_id: str) -> None:
    if msg.talker not in ALLOWED_TALKERS:
        raise NmeaUnsupportedTalkerError(f"unsupported talker: {msg.talker!r}")
    if msg.msgID != expected_msg_id:
        raise NmeaParseError(
            f"expected {expected_msg_id} message, got {msg.msgID!r}"
        )


def _combine_utc_time(t: object, today_utc: date, *, field: str) -> datetime:
    if not isinstance(t, time):
        raise NmeaParseError(f"{field} is missing or not a time")
    return datetime.combine(today_utc, t, tzinfo=UTC)


def _opt_float(x: object) -> float | None:
    """pynmeagps кладёт пустые числовые поля как '' (строку), не None."""
    if isinstance(x, bool):  # bool — подкласс int, отсекаем
        return None
    if isinstance(x, (int, float)):
        return float(x)
    return None
