"""Тесты NMEA-парсера на реальных байтах NMEA-0183.
"""

from datetime import UTC, date, datetime

import pytest
from pynmeagps import NMEAReader

from ntrip_accuracy_monitor.domain.solution_mode import SolutionMode
from ntrip_accuracy_monitor.protocols.nmea import (
    GgaRecord,
    GsaRecord,
    GstRecord,
    NmeaChecksumError,
    NmeaParseError,
    NmeaUnsupportedTalkerError,
    RmcRecord,
    ZdaRecord,
    nmea_to_gga,
    parse_line,
)

TODAY = date(2025, 4, 25)


def _frame(body: str) -> bytes:
    """Собирает полную NMEA-строку с правильной XOR-checksum.

    body — содержимое между '$' и '*' (без них).
    """
    cs = 0
    for ch in body:
        cs ^= ord(ch)
    return f"${body}*{cs:02X}\r\n".encode("ascii")


# ---------- GGA ----------


def test_gga_rtk_fixed_full_fields() -> None:
    raw = _frame(
        "GNGGA,123519.00,5547.43580,N,03739.69640,E,4,12,0.6,180.5,M,14.5,M,1.2,0123"
    )
    rec = parse_line(raw, today_utc=TODAY)

    assert isinstance(rec, GgaRecord)
    assert rec.time_utc == datetime(2025, 4, 25, 12, 35, 19, tzinfo=UTC)
    assert rec.solution_mode is SolutionMode.RTK_FIXED
    assert rec.satellites_used == 12
    assert rec.hdop == pytest.approx(0.6)
    assert rec.age_of_corrections_s == pytest.approx(1.2)
    assert rec.reference_station_id == 123
    assert rec.position is not None
    # 55°47.43580'N = 55 + 47.43580/60 = 55.79059666..°
    assert rec.position.latitude_deg == pytest.approx(55.7905967, abs=1e-6)
    # 037°39.69640'E = 37 + 39.69640/60 = 37.66160666..°
    assert rec.position.longitude_deg == pytest.approx(37.6616067, abs=1e-6)
    assert rec.position.ellipsoidal_height_m == pytest.approx(180.5)


def test_gga_dgps_quality_2() -> None:
    raw = _frame(
        "GNGGA,083102.00,5547.00000,N,03737.00000,E,2,09,1.1,150.0,M,14.5,M,3.5,0001"
    )
    rec = parse_line(raw, today_utc=TODAY)
    assert isinstance(rec, GgaRecord)
    assert rec.solution_mode is SolutionMode.DGNSS
    assert rec.age_of_corrections_s == pytest.approx(3.5)


def test_gga_invalid_quality_zero_allows_empty_position() -> None:
    raw = _frame("GNGGA,083102.00,,,,,0,00,99.99,,M,,M,,")
    rec = parse_line(raw, today_utc=TODAY)
    assert isinstance(rec, GgaRecord)
    assert rec.solution_mode is SolutionMode.INVALID
    assert rec.position is None
    assert rec.age_of_corrections_s is None
    assert rec.reference_station_id is None
    assert rec.satellites_used == 0


def test_gga_quality_1_with_empty_position_raises() -> None:
    """SPP с пустыми lat/lon — это аномалия формата, не валидное состояние."""
    raw = _frame("GNGGA,083102.00,,,,,1,08,1.1,,M,,M,,")
    with pytest.raises(NmeaParseError):
        parse_line(raw, today_utc=TODAY)


def test_gga_with_gp_talker() -> None:
    raw = _frame(
        "GPGGA,123519,4807.038,N,01131.000,E,1,08,0.9,545.4,M,46.9,M,,"
    )
    rec = parse_line(raw, today_utc=TODAY)
    assert isinstance(rec, GgaRecord)
    assert rec.solution_mode is SolutionMode.SPP


# ---------- GST ----------
def test_gst_basic() -> None:
    # NMEA-0183: time, rangeRms, stdMajor, stdMinor, orient, stdLat, stdLng, stdAlt
    raw = _frame("GNGST,172814.00,0.006,0.023,0.020,273.6,0.012,0.018,0.031")
    rec = parse_line(raw, today_utc=TODAY)
    assert isinstance(rec, GstRecord)
    assert rec.time_utc == datetime(2025, 4, 25, 17, 28, 14, tzinfo=UTC)
    assert rec.sigma_north_m == pytest.approx(0.012)  # stdLat
    assert rec.sigma_east_m == pytest.approx(0.018)  # stdLong
    assert rec.sigma_up_m == pytest.approx(0.031)  # stdAlt


# ---------- GSA ----------
def test_gsa_3d_fix() -> None:
    raw = _frame("GPGSA,A,3,04,05,,09,12,,,24,,,,,2.5,1.3,2.1")
    rec = parse_line(raw, today_utc=TODAY)
    assert isinstance(rec, GsaRecord)
    assert rec.fix_type == 3
    assert rec.pdop == pytest.approx(2.5)
    assert rec.hdop == pytest.approx(1.3)
    assert rec.vdop == pytest.approx(2.1)
    assert rec.satellite_prns == (4, 5, 9, 12, 24)


# ---------- RMC ----------
def test_rmc_valid() -> None:
    raw = _frame(
        "GPRMC,123519,A,4807.038,N,01131.000,E,022.4,084.4,230394,003.1,W"
    )
    rec = parse_line(raw, today_utc=TODAY)
    assert isinstance(rec, RmcRecord)
    assert rec.is_valid is True
    # Дата из самого RMC (1994-03-23), today_utc игнорируется
    assert rec.time_utc == datetime(1994, 3, 23, 12, 35, 19, tzinfo=UTC)


def test_rmc_invalid_status_v() -> None:
    raw = _frame(
        "GPRMC,123519,V,4807.038,N,01131.000,E,022.4,084.4,230394,003.1,W"
    )
    rec = parse_line(raw, today_utc=TODAY)
    assert isinstance(rec, RmcRecord)
    assert rec.is_valid is False


# ---------- ZDA ----------
def test_zda() -> None:
    raw = _frame("GPZDA,201530.00,04,07,2002,00,00")
    rec = parse_line(raw, today_utc=TODAY)
    assert isinstance(rec, ZdaRecord)
    assert rec.time_utc == datetime(2002, 7, 4, 20, 15, 30, tzinfo=UTC)


# ---------- error paths ----------
def test_invalid_checksum_raises() -> None:
    # Берём корректную строку и портим checksum
    good = _frame("GPGGA,123519,4807.038,N,01131.000,E,1,08,0.9,545.4,M,46.9,M,,")
    # Заменим *XX в конце на *00
    bad = good[:-5] + b"*00\r\n"
    with pytest.raises(NmeaChecksumError):
        parse_line(bad, today_utc=TODAY)


def test_garbage_input_raises_parse_error() -> None:
    with pytest.raises(NmeaParseError):
        parse_line(b"hello world\r\n", today_utc=TODAY)


def test_unknown_msg_id_returns_none() -> None:
    # GLL — валидная NMEA-сообщение, но не нашего типа
    raw = _frame("GPGLL,4916.45,N,12311.12,W,225444,A")
    assert parse_line(raw, today_utc=TODAY) is None


def test_proprietary_pubx_returns_none() -> None:
    # $PUBX,... — Proprietary u-blox; talker 'P' + 'UBX'.
    # Pynmeagps распарсит структуру; talker не в ALLOWED_TALKERS → None.
    # Если pynmeagps кинет ParseError — тест ожидает либо None, либо NmeaParseError.
    raw = _frame(
        "PUBX,00,083559.00,4717.11437,N,00833.91522,E,546.589,G3,"
        "2.1,2.0,0.007,77.52,0.007,,0.92,1.19,0.77,9,0,0"
    )
    try:
        result = parse_line(raw, today_utc=TODAY)
        assert result is None
    except NmeaParseError:
        pass  # тоже приемлемо: pynmeagps не распознал proprietary формат


def test_accepts_str_and_bytes() -> None:
    body = "GPGGA,123519,4807.038,N,01131.000,E,1,08,0.9,545.4,M,46.9,M,,"
    cs = 0
    for ch in body:
        cs ^= ord(ch)
    sentence = f"${body}*{cs:02X}\r\n"

    rec_from_str = parse_line(sentence, today_utc=TODAY)
    rec_from_bytes = parse_line(sentence.encode("ascii"), today_utc=TODAY)
    assert isinstance(rec_from_str, GgaRecord)
    assert isinstance(rec_from_bytes, GgaRecord)
    assert rec_from_str == rec_from_bytes


def test_sentence_without_crlf_works() -> None:
    body = "GPGGA,123519,4807.038,N,01131.000,E,1,08,0.9,545.4,M,46.9,M,,"
    cs = 0
    for ch in body:
        cs ^= ord(ch)
    sentence = f"${body}*{cs:02X}"  # без \r\n
    rec = parse_line(sentence, today_utc=TODAY)
    assert isinstance(rec, GgaRecord)


# ---------- direct converter ----------
def test_nmea_to_gga_with_wrong_msg_raises() -> None:
    """Прямой вызов nmea_to_gga с RMC-сообщением — программная ошибка."""
    raw = _frame(
        "GPRMC,123519,A,4807.038,N,01131.000,E,022.4,084.4,230394,003.1,W"
    )
    msg = NMEAReader.parse(raw)
    assert msg is not None
    with pytest.raises(NmeaParseError):
        nmea_to_gga(msg, today_utc=TODAY)


def test_nmea_to_gga_unsupported_talker_raises() -> None:
    """Прямой вызов с talker не из ALLOWED_TALKERS."""
    # Используем XX как заведомо неподдерживаемый talker, но валидный по форме
    body = "XXGGA,123519,4807.038,N,01131.000,E,1,08,0.9,545.4,M,46.9,M,,"
    cs = 0
    for ch in body:
        cs ^= ord(ch)
    raw = f"${body}*{cs:02X}\r\n".encode("ascii")
    msg = NMEAReader.parse(raw)
    if msg is None:
        pytest.skip("pynmeagps reject unknown talker before reaching converter")
    with pytest.raises(NmeaUnsupportedTalkerError):
        nmea_to_gga(msg, today_utc=TODAY)
