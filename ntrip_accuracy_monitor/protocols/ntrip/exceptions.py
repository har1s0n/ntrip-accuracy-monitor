"""NTRIP-specific exception hierarchy.

Convention: only NtripPermanentError descendants are surfaced from
NtripClient.__anext__ / .fatal_error. Transient network errors are
absorbed by the supervisor and trigger reconnect via BackoffPolicy.
"""

from __future__ import annotations


class NtripError(Exception):
    """Base exception for all NTRIP transport errors."""


class NtripPermanentError(NtripError):
    """Non-recoverable error.

    NtripClient stops the supervisor and surfaces this exception to the
    consumer; reconnection is NOT attempted.
    """


class NtripSourcetableError(NtripPermanentError):
    """Caster returned a SOURCETABLE response instead of streaming data.

    Typical cause: the requested mountpoint does not exist on the caster,
    or the request was malformed and the caster fell back to publishing
    the sourcetable.
    """


class NtripAuthError(NtripPermanentError):
    """Caster returned 401 Unauthorized.

    The provided credentials are not accepted; retrying without changing
    them is futile.
    """


class NtripMountpointError(NtripPermanentError):
    """Caster returned 404 Not Found for the requested mountpoint."""


class NtripTransientError(NtripError):
    """Recoverable error.

    Currently not raised through the public API; reserved for future
    structured reporting of transient failure causes.
    """
