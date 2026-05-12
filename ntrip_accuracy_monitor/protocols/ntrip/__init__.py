"""Публичный API NTRIP-стека: клиент, кастер, RtcmHub, GGA-провайдеры."""

from ._gga import encode_static_gga, static_gga_provider
from ._hub import RtcmHub
from ._rtcm_source import RtcmSource, TcpRtcmSource, pump
from ._server_handshake import HandshakeError, NtripRequest
from ._sourcetable import StrRecord, build_sourcetable
from .caster import NtripCasterServer
from .exceptions import (
    NtripAuthError,
    NtripMountpointError,
    NtripPermanentError,
    NtripSourcetableError,
)
from .transport import NtripClient

__all__ = [
    # client
    "NtripClient",
    # caster
    "NtripCasterServer",
    "RtcmHub",
    "RtcmSource",
    "TcpRtcmSource",
    "pump",
    # sourcetable & handshake
    "StrRecord",
    "build_sourcetable",
    "NtripRequest",
    "HandshakeError",
    # GGA helpers
    "encode_static_gga",
    "static_gga_provider",
    # exceptions
    "NtripPermanentError",
    "NtripAuthError",
    "NtripMountpointError",
    "NtripSourcetableError",
]
