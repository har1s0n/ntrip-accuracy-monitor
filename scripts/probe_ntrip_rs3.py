"""Field probe for the NtripClient adapter against the EFT RS3 built-in caster.

The EFT RS3 receiver can publish RTCM corrections via its own NTRIP-Caster
service (Web UI → Передача данных → protocol = "Ntrip Caster"). This probe
connects to that caster and validates the same NtripClient code path used
in production by the orchestrator.

The RS3 web UI does not expose its NTRIP protocol version. Older firmwares
implement NTRIP 1.0 only; newer ones support 2.0. This probe attempts both
in `auto` mode (default) and reports which one succeeded.

Required environment variables:

    RS3_USER       NTRIP-Caster user configured on the RS3.
    RS3_PASSWORD   Corresponding password.

Optional flags override the built-in defaults:

    --host         default 192.168.1.40
    --port         default 9002
    --mountpoint   default TESTRS3CAST0
    --duration     default 15.0 seconds
    --version      auto | 1.0 | 2.0   (default: auto)

Run from repo root:

    RS3_USER=...  RS3_PASSWORD=... \\
        uv run python scripts/probe_ntrip_rs3.py
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
import time
from collections import Counter
from dataclasses import dataclass
from typing import Literal, cast

from ntrip_accuracy_monitor.protocols.backoff import BackoffPolicy
from ntrip_accuracy_monitor.protocols.ntrip import (
    NtripAuthError,
    NtripClient,
    NtripMountpointError,
    NtripPermanentError,
    NtripSourcetableError,
)

from ntrip_accuracy_monitor.protocols.ntrip._gga import static_gga_provider

# ---- Defaults specific to this RS3 unit -----------------------------------
# The host is on a local LAN so it is safe to default in the script;
# credentials remain env-only.
_DEFAULT_HOST = "192.168.1.40"
_DEFAULT_PORT = 9002
_DEFAULT_MOUNTPOINT = "TESTRS3CAST0"
_DEFAULT_DURATION_S = 10.0

# Watchdog/connect tunings — tighter than the public-caster probe because
# this is LAN: connect should be sub-second, stall is suspect after 5 s.
_CONNECT_TIMEOUT_S = 5.0
_STALL_TIMEOUT_S = 5.0

_RTCM_PREAMBLE = 0xD3
"""RTCM 3.x frames begin with 0xD3 (RTCM 10403.x §3.5.2)."""

NtripVersion = Literal["1.0", "2.0"]

_log = logging.getLogger("probe_ntrip_rs3")


@dataclass(frozen=True, slots=True)
class _Endpoint:
    host: str
    port: int
    mountpoint: str
    username: str
    password: str


@dataclass(slots=True)
class _SessionResult:
    version: NtripVersion
    frames: int
    bytes_total: int
    elapsed_s: float
    ttf_s: float | None
    msg_counts: Counter[int]
    fatal: NtripPermanentError | None
    reconnects: int
    stall_timeouts: int
    dropped_full: int


def _read_credentials() -> tuple[str, str] | None:
    user = os.environ.get("RS3_USER")
    password = os.environ.get("RS3_PASSWORD")
    if user is None or password is None:
        return None
    return user, password


def _format_message_id(frame: bytes) -> int | None:
    """Extract RTCM 3.x message number (DF002, 12 bits) from a frame."""
    if len(frame) < 6 or frame[0] != _RTCM_PREAMBLE:
        return None
    return (frame[3] << 4) | (frame[4] >> 4)


# ---------------------------------------------------------------------------
# Single attempt with a chosen NTRIP version
# ---------------------------------------------------------------------------

async def _stream_one(
    endpoint: _Endpoint,
    version: NtripVersion,
    duration_s: float,
) -> _SessionResult:
    """Run one streaming session at the given NTRIP version.

    Always returns a result — never raises out. Transient failures and
    permanent fatals are reflected in the result fields. The caller
    decides whether to interpret a session as success.
    """
    _log.info(
        "stream attempt: NTRIP %s @ http://%s:%d/%s",
        version, endpoint.host, endpoint.port, endpoint.mountpoint,
    )

    backoff = BackoffPolicy(
        initial_delay_s=0.5, max_delay_s=2.0, multiplier=2.0, jitter=0.1,
    )

    msg_counts: Counter[int] = Counter()
    total_bytes = 0
    frame_count = 0
    first_frame_at: float | None = None
    started = time.monotonic()
    deadline = started + duration_s
    fatal: NtripPermanentError | None = None
    reconnects = 0
    stall_timeouts = 0
    dropped_full = 0

    try:
        async with NtripClient(
            stream_id=f"probe-rs3-{version}",
            caster_host=endpoint.host,
            caster_port=endpoint.port,
            use_https=False,
            mountpoint=endpoint.mountpoint,
            username=endpoint.username,
            password=endpoint.password,
            ntrip_version=version,
            connect_timeout_s=_CONNECT_TIMEOUT_S,
            stall_timeout_s=_STALL_TIMEOUT_S,
            reconnect_backoff=backoff,
            gga_provider=static_gga_provider(
                lat_deg=55.604362, lon_deg=37.412704, alt_m=243.04482,
            ),
            gga_interval_s=10.0,
        ) as client:
            try:
                async for frame in client:
                    now = time.monotonic()
                    if first_frame_at is None:
                        first_frame_at = now
                        _log.info(
                            "first frame after %.2fs (NTRIP %s)",
                            now - started, version,
                        )
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
                fatal = exc
                _log.warning(
                    "NTRIP %s session ended with fatal: %s: %s",
                    version, type(exc).__name__, exc,
                )
            reconnects = client.reconnects
            stall_timeouts = client.stall_timeouts
            dropped_full = client.dropped_full
    except Exception as exc:  # noqa: BLE001 — surface unexpected errors
        _log.error(
            "NTRIP %s probe raised %s: %s",
            version, type(exc).__name__, exc,
        )

    elapsed = time.monotonic() - started
    ttf = (first_frame_at - started) if first_frame_at is not None else None
    return _SessionResult(
        version=version,
        frames=frame_count,
        bytes_total=total_bytes,
        elapsed_s=elapsed,
        ttf_s=ttf,
        msg_counts=msg_counts,
        fatal=fatal,
        reconnects=reconnects,
        stall_timeouts=stall_timeouts,
        dropped_full=dropped_full,
    )


# ---------------------------------------------------------------------------
# Orchestration: auto-detect version
# ---------------------------------------------------------------------------

async def _run(args: argparse.Namespace) -> int:
    creds = _read_credentials()
    if creds is None:
        print(
            "ERROR: RS3_USER and RS3_PASSWORD environment variables are required.\n"
            "Configure the NTRIP-Caster credentials on the RS3 web UI,\n"
            "then export them before running this probe.",
            file=sys.stderr,
        )
        return 64  # EX_USAGE

    user, password = creds
    endpoint = _Endpoint(
        host=args.host,
        port=args.port,
        mountpoint=args.mountpoint,
        username=user,
        password=password,
    )

    # Decide which versions to try.
    versions_to_try: list[NtripVersion]
    if args.version == "auto":
        # Order matters: 2.0 first because it tells us the more capable
        # firmware path; if RS3 silently downgrades, 1.0 still works.
        versions_to_try = ["2.0", "1.0"]
    else:
        versions_to_try = [cast(NtripVersion, args.version)]

    results: list[_SessionResult] = []
    for ver in versions_to_try:
        # Each attempt gets a fraction of the requested duration in auto
        # mode so the total wall-clock stays close to the user's intent.
        per_attempt = (
            args.duration / len(versions_to_try)
            if args.version == "auto"
            else args.duration
        )
        result = await _stream_one(endpoint, ver, per_attempt)
        results.append(result)
        if result.frames > 0 and result.fatal is None:
            # Working version found; no need to try the others.
            break
        # If we got an auth error, neither version will work — bail early.
        if isinstance(result.fatal, NtripAuthError):
            _log.error("auth rejected on NTRIP %s; aborting auto-detect", ver)
            break

    _print_summary(endpoint, results, args.version)
    # Exit status: 0 if any attempt produced frames without fatal.
    return 0 if any(r.frames > 0 and r.fatal is None for r in results) else 1


def _print_summary(
    endpoint: _Endpoint,
    results: list[_SessionResult],
    requested_version: str,
) -> None:
    print(f"\n=== RS3 probe: http://{endpoint.host}:{endpoint.port}"
          f"/{endpoint.mountpoint} ===")
    print(f"requested version: {requested_version}")

    winner = next(
        (r for r in results if r.frames > 0 and r.fatal is None),
        None,
    )
    if winner is not None:
        print(f"working version:   NTRIP {winner.version}")
    else:
        print("working version:   NONE — see attempt details below")

    for r in results:
        rate_fps = r.frames / r.elapsed_s if r.elapsed_s > 0 else 0.0
        rate_kbps = (r.bytes_total / 1024.0) / r.elapsed_s if r.elapsed_s > 0 else 0.0
        print(f"\n--- attempt: NTRIP {r.version} ---")
        print(f"  elapsed:         {r.elapsed_s:.2f} s")
        print(f"  frames:          {r.frames}")
        print(f"  bytes:           {r.bytes_total}")
        print(f"  throughput:      {rate_fps:.2f} frames/s, {rate_kbps:.2f} KiB/s")
        ttf_str = f"{r.ttf_s:.2f} s" if r.ttf_s is not None else "n/a (no frames)"
        print(f"  first frame at:  {ttf_str}")
        print(f"  reconnects:      {r.reconnects}")
        print(f"  stall timeouts:  {r.stall_timeouts}")
        print(f"  queue overflows: {r.dropped_full}")
        if r.fatal is not None:
            print(f"  fatal:           {type(r.fatal).__name__}: {r.fatal}")
        else:
            print("  fatal:           None")
        if r.msg_counts:
            print("  RTCM messages observed:")
            for msg_id, n in sorted(r.msg_counts.items()):
                print(f"    {msg_id:4d} : {n:6d}  ({_msg_hint(msg_id)})")


def _msg_hint(msg_id: int) -> str:
    """Quick-reference labels for common RTCM 3.x message IDs."""
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
# CLI
# ---------------------------------------------------------------------------

def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Probe NtripClient against the EFT RS3 built-in NTRIP caster.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--host", default=_DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=_DEFAULT_PORT)
    parser.add_argument("--mountpoint", default=_DEFAULT_MOUNTPOINT)
    parser.add_argument(
        "--duration", type=float, default=_DEFAULT_DURATION_S,
        help="Total streaming duration; split across attempts in 'auto' mode.",
    )
    parser.add_argument(
        "--version",
        choices=("auto", "1.0", "2.0"),
        default="1.0",
        help="NTRIP version: auto-detect (default), or force 1.0 / 2.0.",
    )
    parser.add_argument(
        "--log-level",
        choices=("DEBUG", "INFO", "WARNING", "ERROR"),
        default="INFO",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=args.log_level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
