#!/usr/bin/env python3
"""Подключается к одному EFT RS3, печатает приходящие NMEA-записи в stdout,
логи транспорта — в stderr. По Ctrl+C — корректное завершение и сводка счётчиков.

Запуск:
    uv run python scripts/probe_nmea_tcp.py --host 192.168.X.Y
    uv run python scripts/probe_nmea_tcp.py --host 192.168.X.Y --log-level DEBUG
    uv run python scripts/probe_nmea_tcp.py --host 192.168.X.Y --max-records 50
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import time

from ntrip_accuracy_monitor.protocols.backoff import BackoffPolicy
from ntrip_accuracy_monitor.protocols.nmea.messages import NmeaRecord
from ntrip_accuracy_monitor.protocols.nmea.transport import NmeaTcpClient


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Probe NmeaTcpClient against a live RS3 receiver.")
    p.add_argument("--host", required=True, help="receiver IP / hostname")
    p.add_argument("--port", type=int, default=9001)
    p.add_argument("--stream-id", default="probe")
    p.add_argument("--connect-timeout", type=float, default=5.0)
    p.add_argument("--stall-timeout", type=float, default=10.0)
    p.add_argument("--initial-backoff", type=float, default=1.0)
    p.add_argument("--max-backoff", type=float, default=30.0)
    p.add_argument("--max-records", type=int, default=0,
                   help="0 = run until Ctrl+C")
    p.add_argument("--log-level", default="INFO",
                   choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    return p.parse_args()


def _setup_logging(level_name: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level_name),
        stream=sys.stderr,
        format="%(asctime)s.%(msecs)03d [%(levelname)-7s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


def _format(rec: NmeaRecord) -> str:
    # Универсально: используем штатный repr frozen-dataclass'а.
    return f"{type(rec).__name__:11s} {rec}"


def _print_summary(client: NmeaTcpClient, count: int, elapsed_s: float) -> None:
    rate = count / elapsed_s if elapsed_s > 0 else 0.0
    print("\n--- summary ---", file=sys.stderr)
    print(f"records received:    {count}", file=sys.stderr)
    print(f"elapsed:             {elapsed_s:.2f}s ({rate:.2f} rec/s)", file=sys.stderr)
    print(f"parse_errors:        {client.parse_errors}", file=sys.stderr)
    print(f"checksum_failures:   {client.checksum_failures}", file=sys.stderr)
    print(f"non_nmea_lines:      {client.non_nmea_lines}", file=sys.stderr)
    print(f"reconnects:          {client.reconnects}", file=sys.stderr)


async def _run(args: argparse.Namespace) -> int:
    backoff = BackoffPolicy(
        initial_delay_s=args.initial_backoff,
        max_delay_s=args.max_backoff,
        multiplier=2.0,
        jitter=0.1,
    )
    client = NmeaTcpClient(
        stream_id=args.stream_id,
        host=args.host,
        port=args.port,
        connect_timeout_s=args.connect_timeout,
        stall_timeout_s=args.stall_timeout,
        backoff=backoff,
    )

    count = 0
    started = time.monotonic()
    try:
        async with client:
            async for rec in client:
                count += 1
                ts = time.monotonic() - started
                print(f"[{ts:8.3f}s] #{count:6d}  {_format(rec)}", flush=True)
                if args.max_records and count >= args.max_records:
                    break
    finally:
        _print_summary(client, count, time.monotonic() - started)
    return 0


def main() -> int:
    args = _parse_args()
    _setup_logging(args.log_level)
    try:
        return asyncio.run(_run(args))
    except KeyboardInterrupt:
        # asyncio.run уже отменил задачу и прогнал finally — здесь только код возврата.
        return 130


if __name__ == "__main__":
    sys.exit(main())
