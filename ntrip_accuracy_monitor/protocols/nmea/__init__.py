"""Публичный API NMEA-адаптера."""

from ntrip_accuracy_monitor.protocols.nmea.transport import (
    MAX_NMEA_LINE_LEN,
    NmeaTcpClient,
)

from .errors import (
    NmeaChecksumError,
    NmeaError,
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
from .parser import (
    ALLOWED_TALKERS,
    nmea_to_gga,
    nmea_to_gsa,
    nmea_to_gst,
    nmea_to_rmc,
    nmea_to_zda,
    parse_line,
)

__all__ = [
    "ALLOWED_TALKERS",
    "MAX_NMEA_LINE_LEN",
    "GgaRecord",
    "GsaRecord",
    "GstRecord",
    "NmeaChecksumError",
    "NmeaError",
    "NmeaParseError",
    "NmeaRecord",
    "NmeaTcpClient",
    "NmeaUnsupportedTalkerError",
    "RmcRecord",
    "ZdaRecord",
    "nmea_to_gga",
    "nmea_to_gsa",
    "nmea_to_gst",
    "nmea_to_rmc",
    "nmea_to_zda",
    "parse_line",
]
