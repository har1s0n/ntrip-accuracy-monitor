"""Field probes for the NtripClient adapter against a public NTRIP caster.

Default target is BKG's EUREF-IP infrastructure:
  - euref-ip.bkg.bund.de:2101 (Frankfurt)
  - euref-ip.asi.it:2101      (Italy mirror)
Both are reasonable from Warsaw; ASI tends to have lower latency.

Three subcommands:

    sourcetable  Pull the caster's sourcetable directly via pygnssutils
                 (NtripClient does NOT permit empty mountpoints, since
                 production code never wants the sourcetable). Prints a
                 summary plus first STR entries near Poland.

    not-found    Connect with a deliberately-bogus mountpoint, expect
                 NtripMountpointError on a NTRIP 2.0 caster (BKG returns
                 HTTP 404; older NTRIP 1.0 casters may return SOURCETABLE
                 in which case NtripSourcetableError is the success).

    stream       Connect to a real mountpoint, consume RTCM for a fixed
                 duration, print frame/byte counts, throughput, and a
                 tally of RTCM message types observed.

Credentials come from the environment ONLY (NTRIP_USER, NTRIP_PASSWORD),
required for `stream` only.

Run examples:

    uv run python scripts/probe_ntrip_public.py sourcetable
    uv run python scripts/probe_ntrip_public.py not-found
    NTRIP_USER=... NTRIP_PASSWORD=... \\
        uv run python scripts/probe_ntrip_public.py stream \\
            --mountpoint WROC00POL0 --duration 15
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import queue as _queue
import sys
import threading
import time
from collections import Counter
from dataclasses import dataclass

from pygnssutils import GNSSNTRIPClient

from ntrip_accuracy_monitor.protocols.backoff import BackoffPolicy
from ntrip_accuracy_monitor.protocols.ntrip import (
    NtripAuthError,
    NtripClient,
    NtripMountpointError,
    NtripPermanentError,
    NtripSourcetableError,
)

_DEFAULT_HOST = "euref-ip.net"
_DEFAULT_PORT = 2101
_DEFAULT_VERSION = "2.0"
_DEFAULT_DURATION_S = 15.0
_DEFAULT_STALL_S = 12.0
_DEFAULT_CONNECT_S = 10.0

_BOGUS_MOUNTPOINT = "NOSUCH00POL0"
"""IGS-style 9-char placeholder; deliberately absent from any normal caster."""

_RTCM_PREAMBLE = 0xD3
"""RTCM 3.x frames begin with 0xD3 (RTCM 10403.x §3.5.2)."""

_log = logging.getLogger("probe_ntrip_public")


@dataclass(frozen=True, slots=True)
class _Endpoint:
    host: str
    port: int
    use_https: bool
    version: str  # "1.0" | "2.0"


def _parse_endpoint(args: argparse.Namespace) -> _Endpoint:
    return _Endpoint(
        host=args.host,
        port=args.port,
        use_https=args.https,
        version=args.ntrip_version,
    )


def _read_credentials() -> tuple[str | None, str | None]:
    return os.environ.get("NTRIP_USER"), os.environ.get("NTRIP_PASSWORD")


def _format_message_id(frame: bytes) -> int | None:
    """Extract RTCM 3.x message number (DF002, 12 bits) from a frame."""
    if len(frame) < 6 or frame[0] != _RTCM_PREAMBLE:
        return None
    df002 = (frame[3] << 4) | (frame[4] >> 4)
    return df002


# ---------------------------------------------------------------------------
# Subcommand: sourcetable  (CHANGED: now uses pygnssutils directly)
# ---------------------------------------------------------------------------

def _run_sourcetable(endpoint: _Endpoint) -> int:
    """Pull the sourcetable directly via pygnssutils (synchronous).

    NtripClient intentionally rejects empty mountpoints — production code
    never wants the sourcetable. For an ad-hoc directory listing we go
    straight to GNSSNTRIPClient with mountpoint="", which is the standard
    way to request a sourcetable from any NTRIP caster.

    Args:
        endpoint: caster connection parameters.

    Returns:
        0 if any sourcetable content was captured, 1 otherwise.
    """
    _log.info(
        "fetching sourcetable from %s://%s:%d",
        "https" if endpoint.use_https else "http",
        endpoint.host,
        endpoint.port,
    )

    captured_logs: list[str] = []
    captured_table: list[bytes] = []

    class _LogCapture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            try:
                captured_logs.append(record.getMessage())
            except Exception:  # noqa: BLE001
                pass

    pygnss_logger = logging.getLogger("pygnssutils.gnssntripclient")
    handler = _LogCapture(level=logging.DEBUG)
    pygnss_logger.addHandler(handler)
    prev_level = pygnss_logger.level
    pygnss_logger.setLevel(logging.DEBUG)

    # pygnssutils writes the sourcetable to its `output` queue when the
    # mountpoint is empty. We give it a queue.Queue so the isinstance
    # check inside the library succeeds.
    output_q: _queue.Queue[object] = _queue.Queue()
    stopevent = threading.Event()

    started = time.monotonic()
    try:
        with GNSSNTRIPClient() as gnc:
            gnc.run(
                server=endpoint.host,
                port=endpoint.port,
                https=1 if endpoint.use_https else 0,
                mountpoint="",
                version=endpoint.version,
                ntripuser="anonymous",
                ntrippassword="",
                datatype="RTCM",
                ggainterval=-1,
                output=output_q,
                stopevent=stopevent,
                retries=0,
                timeout=int(_DEFAULT_STALL_S),
            )
            # Drain the queue for up to ~5s; library closes its read
            # thread after the sourcetable is delivered.
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline and not stopevent.is_set():
                try:
                    item = output_q.get(timeout=0.2)
                except _queue.Empty:
                    continue
                if isinstance(item, tuple) and item:
                    payload = item[0]
                    if isinstance(payload, (bytes, bytearray)):
                        captured_table.append(bytes(payload))
                elif isinstance(item, (bytes, bytearray)):
                    captured_table.append(bytes(item))
            stopevent.set()
    except Exception as exc:  # noqa: BLE001
        _log.error("sourcetable probe failed: %s: %s", type(exc).__name__, exc)
        return 2
    finally:
        pygnss_logger.removeHandler(handler)
        pygnss_logger.setLevel(prev_level)

    elapsed = time.monotonic() - started
    print(f"\n=== sourcetable probe: {elapsed:.1f}s ===")

    table_blob = b"".join(captured_table)
    if table_blob:
        text = table_blob.decode("ascii", errors="replace")
        str_lines = [ln for ln in text.splitlines() if ln.startswith("STR;")]
        cas_lines = [ln for ln in text.splitlines() if ln.startswith("CAS;")]
        net_lines = [ln for ln in text.splitlines() if ln.startswith("NET;")]
        print(f"sourcetable bytes: {len(table_blob)}")
        print(f"  CAS entries:  {len(cas_lines)}")
        print(f"  NET entries:  {len(net_lines)}")
        print(f"  STR entries:  {len(str_lines)}")
        # Filter for stations near Poland by ITRF country code in field 9.
        # Layout: STR;mount;identifier;format;format-details;carrier;
        #         nav-system;network;country;lat;lon;...
        nearby = [
            ln for ln in str_lines
            if _country_field(ln) in {"POL", "DEU", "CZE", "SVK", "BLR", "LTU", "UKR"}
        ]
        if nearby:
            print(f"\nFirst {min(10, len(nearby))} STR entries near Poland:")
            for ln in nearby[:10]:
                print(f"  {ln[:160]}")
        else:
            print(f"\nFirst {min(10, len(str_lines))} STR entries (any country):")
            for ln in str_lines[:10]:
                print(f"  {ln[:160]}")
        return 0

    print("[!!] no sourcetable bytes captured.")
    if captured_logs:
        print("\nFirst 5 captured pygnssutils log lines:")
        for line in captured_logs[:5]:
            print(f"  {line[:160]}")
    return 1


def _country_field(str_line: str) -> str:
    """Return the country field (index 8) from a STR;... sourcetable row."""
    parts = str_line.split(";")
    return parts[8] if len(parts) > 8 else ""


# ---------------------------------------------------------------------------
# Subcommand: not-found  (CHANGED: was the old "sourcetable" semantics)
# ---------------------------------------------------------------------------

async def _run_not_found(endpoint: _Endpoint) -> int:
    """Connect with a bogus mountpoint and expect a permanent fatal.

    Validates that NtripClient correctly classifies and surfaces server
    rejections. On NTRIP 2.0 casters (e.g. BKG) this yields a clean
    HTTP 404 -> NtripMountpointError. Older NTRIP 1.0 casters that fall
    back to SOURCETABLE -> NtripSourcetableError; both are accepted as
    success here because both indicate a working fatal-error path.
    """
    _log.info(
        "probing fatal-path with bogus mountpoint '%s' on %s://%s:%d",
        _BOGUS_MOUNTPOINT,
        "https" if endpoint.use_https else "http",
        endpoint.host,
        endpoint.port,
    )

    backoff = BackoffPolicy(
        initial_delay_s=1.0, max_delay_s=2.0, multiplier=2.0, jitter=0.0,
    )

    fatal: NtripPermanentError | None = None
    started = time.monotonic()
    try:
        async with NtripClient(
            stream_id="probe-not-found",
            caster_host=endpoint.host,
            caster_port=endpoint.port,
            use_https=endpoint.use_https,
            mountpoint=_BOGUS_MOUNTPOINT,
            username=None,
            password=None,
            ntrip_version=endpoint.version,  # type: ignore[arg-type]
            connect_timeout_s=_DEFAULT_CONNECT_S,
            stall_timeout_s=_DEFAULT_STALL_S,
            reconnect_backoff=backoff,
        ) as client:
            try:
                async for _ in client:
                    _log.warning("unexpected RTCM frame on not-found probe")
                    break
            except NtripPermanentError as exc:
                fatal = exc
    except Exception as exc:  # noqa: BLE001
        _log.error("probe failed unexpectedly: %s: %s", type(exc).__name__, exc)
        return 2

    elapsed = time.monotonic() - started
    print(f"\n=== not-found probe: {elapsed:.1f}s ===")
    if isinstance(fatal, NtripMountpointError):
        print("[OK] adapter surfaced NtripMountpointError "
              "(NTRIP 2.0 caster honest-404 path)")
        return 0
    if isinstance(fatal, NtripSourcetableError):
        print("[OK] adapter surfaced NtripSourcetableError "
              "(NTRIP 1.0 sourcetable-fallback path)")
        return 0
    if isinstance(fatal, NtripAuthError):
        print(f"[!!] caster requires auth before lookup: {fatal}")
        return 1
    if fatal is None:
        print("[!!] no fatal raised; adapter behavior may have regressed")
        return 1
    print(f"[??] unexpected fatal type {type(fatal).__name__}: {fatal}")
    return 1


# ---------------------------------------------------------------------------
# Subcommand: stream  (unchanged)
# ---------------------------------------------------------------------------

async def _run_stream(
    endpoint: _Endpoint,
    mountpoint: str,
    duration_s: float,
) -> int:
    user, password = _read_credentials()
    if user is None or password is None:
        print(
            "ERROR: NTRIP_USER and NTRIP_PASSWORD environment variables are "
            "required for stream mode.\n"
            "Register a free account at https://register.rtcm-ntrip.org/ ,\n"
            "then export both variables before running this probe.",
            file=sys.stderr,
        )
        return 64  # EX_USAGE

    _log.info(
        "streaming %s from %s://%s:%d for %.1fs",
        mountpoint,
        "https" if endpoint.use_https else "http",
        endpoint.host,
        endpoint.port,
        duration_s,
    )

    backoff = BackoffPolicy(
        initial_delay_s=1.0, max_delay_s=8.0, multiplier=2.0, jitter=0.1,
    )

    msg_counts: Counter[int] = Counter()
    total_bytes = 0
    frame_count = 0
    first_frame_at: float | None = None
    started = time.monotonic()
    deadline = started + duration_s
    try:
        async with NtripClient(
            stream_id="probe-stream",
            caster_host=endpoint.host,
            caster_port=endpoint.port,
            use_https=endpoint.use_https,
            mountpoint=mountpoint,
            username=user,
            password=password,
            ntrip_version=endpoint.version,  # type: ignore[arg-type]
            connect_timeout_s=_DEFAULT_CONNECT_S,
            stall_timeout_s=_DEFAULT_STALL_S,
            reconnect_backoff=backoff,
        ) as client:
            try:
                async for frame in client:
                    now = time.monotonic()
                    if first_frame_at is None:
                        first_frame_at = now
                        _log.info("first frame after %.2fs", now - started)
                    frame_count += 1
                    total_bytes += len(frame)
                    msg_id = _format_message_id(frame)
                    if msg_id is not None:
                        msg_counts[msg_id] += 1
                    if frame_count <= 3:
                        head = frame[:16].hex()
                        _log.info(
                            "frame #%d: %d bytes, msg=%s, head=%s...",
                            frame_count, len(frame),
                            msg_id if msg_id is not None else "??",
                            head,
                        )
                    if now >= deadline:
                        break
            except NtripPermanentError as exc:
                _log.error("fatal: %s: %s", type(exc).__name__, exc)
                return 1

            elapsed = time.monotonic() - started
            ttf = (first_frame_at - started) if first_frame_at is not None else None
            _print_stream_summary(
                client=client,
                mountpoint=mountpoint,
                elapsed_s=elapsed,
                frame_count=frame_count,
                total_bytes=total_bytes,
                msg_counts=msg_counts,
                ttf_s=ttf,
            )
    except Exception as exc:  # noqa: BLE001
        _log.error("probe failed: %s: %s", type(exc).__name__, exc)
        return 2
    return 0 if frame_count > 0 else 1


def _print_stream_summary(
    *,
    client: NtripClient,
    mountpoint: str,
    elapsed_s: float,
    frame_count: int,
    total_bytes: int,
    msg_counts: Counter[int],
    ttf_s: float | None,
) -> None:
    rate_fps = frame_count / elapsed_s if elapsed_s > 0 else 0.0
    rate_kbps = (total_bytes / 1024.0) / elapsed_s if elapsed_s > 0 else 0.0
    print(f"\n=== stream probe: {mountpoint} ===")
    print(f"elapsed:           {elapsed_s:.2f} s")
    print(f"frames:            {frame_count}")
    print(f"bytes:             {total_bytes}")
    print(f"throughput:        {rate_fps:.2f} frames/s, {rate_kbps:.2f} KiB/s")
    print(f"first frame at:    {ttf_s:.2f} s" if ttf_s is not None else "first frame at:    n/a")
    print(f"reconnects:        {client.reconnects}")
    print(f"stall timeouts:    {client.stall_timeouts}")
    print(f"queue overflows:   {client.dropped_full}")
    print(f"fatal_error:       {client.fatal_error}")
    if msg_counts:
        print("\nRTCM message types observed:")
        for msg_id, n in sorted(msg_counts.items()):
            print(f"  {msg_id:4d} : {n:6d}  ({_msg_hint(msg_id)})")


def _msg_hint(msg_id: int) -> str:
    table: dict[int, str] = {
        1004: "GPS L1/L2 obs (extended)",
        1005: "Stationary RTK Reference (no antenna height)",
        1006: "Stationary RTK Reference (with antenna height)",
        1007: "Antenna descriptor",
        1008: "Antenna descriptor + serial",
        1012: "GLONASS L1/L2 obs (extended)",
        1013: "System parameters",
        1019: "GPS ephemerides",
        1020: "GLONASS ephemerides",
        1033: "Receiver and antenna descriptors",
        1042: "BeiDou ephemerides",
        1045: "Galileo F/NAV ephemerides",
        1046: "Galileo I/NAV ephemerides",
        1074: "GPS MSM4",
        1075: "GPS MSM5",
        1077: "GPS MSM7",
        1084: "GLONASS MSM4",
        1085: "GLONASS MSM5",
        1087: "GLONASS MSM7",
        1094: "Galileo MSM4",
        1095: "Galileo MSM5",
        1097: "Galileo MSM7",
        1115: "QZSS MSM5",
        1117: "QZSS MSM7",
        1124: "BeiDou MSM4",
        1125: "BeiDou MSM5",
        1127: "BeiDou MSM7",
        1230: "GLONASS code-phase biases",
    }
    return table.get(msg_id, "—")


# ---------------------------------------------------------------------------
# CLI entry
# ---------------------------------------------------------------------------

def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Probe the NtripClient adapter against a public NTRIP caster.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--host", default=_DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=_DEFAULT_PORT)
    parser.add_argument("--https", action="store_true")
    parser.add_argument(
        "--ntrip-version",
        choices=("1.0", "2.0"),
        default=_DEFAULT_VERSION,
    )
    parser.add_argument(
        "--log-level",
        choices=("DEBUG", "INFO", "WARNING", "ERROR"),
        default="INFO",
    )

    subs = parser.add_subparsers(dest="cmd", required=True)
    subs.add_parser(
        "sourcetable",
        help="Pull caster sourcetable; print STR entries near Poland.",
    )
    subs.add_parser(
        "not-found",
        help="Probe with a bogus mountpoint; expect a permanent fatal.",
    )
    p_stream = subs.add_parser(
        "stream",
        help="Stream RTCM from a real mountpoint for a fixed duration.",
    )
    p_stream.add_argument("--mountpoint", required=True)
    p_stream.add_argument(
        "--duration", type=float, default=_DEFAULT_DURATION_S,
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=args.log_level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    endpoint = _parse_endpoint(args)

    if args.cmd == "sourcetable":
        return _run_sourcetable(endpoint)
    if args.cmd == "not-found":
        return asyncio.run(_run_not_found(endpoint))
    if args.cmd == "stream":
        return asyncio.run(
            _run_stream(endpoint, args.mountpoint, args.duration),
        )
    parser.error(f"unknown command: {args.cmd}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
