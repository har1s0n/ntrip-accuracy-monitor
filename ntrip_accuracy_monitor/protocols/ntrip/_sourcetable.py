"""Минимальный sourcetable formatter (RTCM 10410.1 p.6.3 STR-record).

Формат строки STR — 18 полей через ';':
  STR;mountpoint;identifier;format;format-details;carrier;nav-system;
      network;country;latitude;longitude;nmea;solution;generator;
      compr-encrp;authentication;fee;bitrate;misc

Тело завершается строкой "ENDSOURCETABLE".
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class StrRecord:
    mountpoint: str
    identifier: str = "RS3-LOCAL"
    rtcm_format: str = "RTCM 3.3"
    format_details: str = "1004(1),1006(10),1012(1),1019,1020,1033(10)"
    carrier: int = 2  # 0=no carrier, 1=L1, 2=L1+L2
    nav_system: str = "GPS+GLO"
    network: str = "PRIVATE"
    country: str = "POL"
    latitude: float = 0.0
    longitude: float = 0.0
    nmea: int = 0  # 0 — клиенту GGA-uplink не нужен (база, не VRS)
    solution: int = 0  # 0 = single base
    generator: str = "ntrip-accuracy-monitor"
    compr_encrp: str = "none"
    authentication: str = "B"  # B=Basic, N=none
    fee: str = "N"
    bitrate: int = 9600
    misc: str = "none"

    def to_line(self) -> str:
        fields = [
            "STR",
            self.mountpoint,
            self.identifier,
            self.rtcm_format,
            self.format_details,
            str(self.carrier),
            self.nav_system,
            self.network,
            self.country,
            f"{self.latitude:.4f}",
            f"{self.longitude:.4f}",
            str(self.nmea),
            str(self.solution),
            self.generator,
            self.compr_encrp,
            self.authentication,
            self.fee,
            str(self.bitrate),
            self.misc,
        ]
        return ";".join(fields)


def build_sourcetable(records: list[StrRecord]) -> bytes:
    """Сформировать тело sourcetable, готовое к отправке."""
    lines = [r.to_line() for r in records]
    lines.append("ENDSOURCETABLE")
    return ("\r\n".join(lines) + "\r\n").encode("ascii")
