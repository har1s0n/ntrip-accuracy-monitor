"""Боевая верификация upstream NTRIP → наш каст → control client.

Параметры — из config.toml + .env, кроме длительности и пути к конфигу.

Запуск:
    uv run python -m scripts.probe_caster_relay --duration 60

Параллельно — перенастроить RS3 #2/#3 в web-UI как Ntrip-клиентов локального
каста (host: IP хоста мониторинга; port/mountpoint: caster.* из config.toml),
и наблюдать GGA quality 1 → 5 → 4 на ровере.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

from ntrip_accuracy_monitor.application.config import AppConfig, BackoffConfig, load_config
from ntrip_accuracy_monitor.protocols.ntrip import (
    NtripCasterServer,
    NtripClient,
    RtcmHub,
)
from ntrip_accuracy_monitor.protocols.rtcm import RtcmAdapter, RtcmParseError
from ntrip_accuracy_monitor.protocols.ntrip._gga import static_gga_provider

logger = logging.getLogger("probe_caster_relay")


@dataclass(slots=True)
class Stats:
    upstream_frames: int = 0
    downstream_frames: int = 0
    upstream_types: Counter[int] = field(default_factory=Counter)
    downstream_types: Counter[int] = field(default_factory=Counter)
    upstream_parse_errors: int = 0
    downstream_parse_errors: int = 0


def _resolve_upstream_endpoint(url: str) -> tuple[str, int, bool]:
    """AnyUrl-stringified → (host, port, use_https)."""
    parsed = urlparse(url)
    host = parsed.hostname
    if not host:
        raise ValueError(f"upstream.url не содержит host: {url!r}")
    use_https = parsed.scheme == "https"
    port = parsed.port if parsed.port is not None else (443 if use_https else 2101)
    return host, port, use_https


async def run_upstream(*, cfg: AppConfig, hub: RtcmHub, stats: Stats) -> None:
    up = cfg.upstream_ntrip
    if not up.enabled:
        raise RuntimeError(
            "upstream.enabled = false. Включи в config.toml и задай "
            "UPSTREAM_NTRIP_PASSWORD в .env, иначе верифицировать релай нечем."
        )
    # model_validator в NtripUpstreamConfig гарантирует url/mountpoint при enabled=True.
    assert up.url is not None and up.mountpoint is not None

    host, port, use_https = _resolve_upstream_endpoint(str(up.url))
    pwd = up.password.get_secret_value() if up.password is not None else ""

    logger.info(
        "Upstream: %s://%s:%d/%s (NTRIP %s, user=%s)",
        "https" if use_https else "http",
        host, port, up.mountpoint, up.ntrip_version,
        up.user or "<none>",
    )

    async with NtripClient(
        stream_id="upstream",
        caster_host=host,
        caster_port=port,
        use_https=use_https,
        mountpoint=up.mountpoint,
        username=up.user or "",
        password=pwd,
        ntrip_version=up.ntrip_version,
        connect_timeout_s=up.connect_timeout_s,
        stall_timeout_s=up.stall_timeout_s,
        queue_max_size=up.queue_max_size,
        reconnect_backoff=up.backoff.to_policy(),
        gga_provider=static_gga_provider(
            lat_deg=55.604362, lon_deg=37.412704, alt_m=243.04482,
        ),
        gga_interval_s=10.0,
    ) as upstream:
        adapter = RtcmAdapter()
        async for frame in upstream:
            hub.feed(frame)
            stats.upstream_frames += 1
            try:
                msg = adapter.parse(frame)
                stats.upstream_types[msg.message_type] += 1
            except RtcmParseError:
                stats.upstream_parse_errors += 1


async def run_control_client(*, cfg: AppConfig, stats: Stats) -> None:
    cas = cfg.local_caster
    username = cas.username or ""
    password = cas.password.get_secret_value() if cas.password is not None else ""

    # Дать кастеру и upstream-у раскачаться. 2 секунды — с запасом.
    await asyncio.sleep(2.0)

    async with NtripClient(
        stream_id="control",
        caster_host=cas.host,
        caster_port=cas.port,
        use_https=False,
        mountpoint=cas.mountpoint,
        username=username,
        password=password,
        ntrip_version="1.0",  # как делают сами RS3
        connect_timeout_s=5.0,  # loopback — соединение мгновенное
        stall_timeout_s=15.0,  # если за 15 с пусто — что-то сломалось
        reconnect_backoff=BackoffConfig().to_policy(),  # default 1s→60s×2; control живёт коротко
    ) as control:
        adapter = RtcmAdapter()
        async for frame in control:
            stats.downstream_frames += 1
            try:
                msg = adapter.parse(frame)
                stats.downstream_types[msg.message_type] += 1
            except RtcmParseError:
                stats.downstream_parse_errors += 1


async def report_loop(stats: Stats, *, interval: float = 5.0) -> None:
    while True:
        await asyncio.sleep(interval)
        up_t = ",".join(f"{k}:{v}" for k, v in sorted(stats.upstream_types.items()))
        dn_t = ",".join(f"{k}:{v}" for k, v in sorted(stats.downstream_types.items()))
        logger.info(
            "upstream=%d [%s] | downstream=%d [%s] | parse_err up=%d dn=%d",
            stats.upstream_frames, up_t,
            stats.downstream_frames, dn_t,
            stats.upstream_parse_errors, stats.downstream_parse_errors,
        )


async def amain(*, config_path: Path, duration: float) -> int:
    cfg = load_config(config_path)

    logging.basicConfig(
        level=getattr(logging, cfg.log_level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s — %(message)s",
    )
    logger.info("Loaded config from %s", config_path)

    stats = Stats()
    hub = RtcmHub(subscriber_queue_size=cfg.local_caster.subscriber_queue_size)

    cas = cfg.local_caster
    cas_pwd = cas.password.get_secret_value() if cas.password is not None else None

    upstream_t: asyncio.Task[None] | None = None
    control_t: asyncio.Task[None] | None = None
    report_t: asyncio.Task[None] | None = None

    try:
        async with NtripCasterServer(
            host=cas.host,
            port=cas.port,
            mountpoint=cas.mountpoint,
            hub=hub,
            username=cas.username,
            password=cas_pwd,
            sourcetable_country=cas.sourcetable_country,
            handshake_timeout_s=cas.handshake_timeout_s,
        ) as caster:
            logger.info(
                "Local caster: %s:%d/%s (auth=%s). RS3 ровер подключай сюда.",
                cas.host, cas.port, cas.mountpoint,
                "yes" if (cas.username or cas.password) else "no",
            )

            upstream_t = asyncio.create_task(run_upstream(cfg=cfg, hub=hub, stats=stats))
            control_t = asyncio.create_task(run_control_client(cfg=cfg, stats=stats))
            report_t = asyncio.create_task(report_loop(stats))

            try:
                await asyncio.wait_for(
                    asyncio.gather(upstream_t, control_t),
                    timeout=duration,
                )
            except TimeoutError:
                logger.info("Длительность %.0fs истекла — останавливаемся.", duration)
    finally:
        for t in (upstream_t, control_t, report_t):
            if t is not None and not t.done():
                t.cancel()
        await asyncio.gather(
            *(t for t in (upstream_t, control_t, report_t) if t is not None),
            return_exceptions=True,
        )

    # ---- финальная сводка ----
    logger.info("=" * 70)
    logger.info("FINAL upstream:   %d frames, types=%s, parse_err=%d",
                stats.upstream_frames, dict(stats.upstream_types), stats.upstream_parse_errors)
    logger.info("FINAL downstream: %d frames, types=%s, parse_err=%d",
                stats.downstream_frames, dict(stats.downstream_types), stats.downstream_parse_errors)
    logger.info("FINAL caster:     accepted=%d authorized=%d 401=%d 404=%d ST_served=%d",
                caster.clients_accepted, caster.clients_authorized,
                caster.clients_rejected_auth, caster.clients_rejected_404,
                caster.sourcetable_served)
    logger.info("FINAL hub:        fed=%d total_dropped=%d",
                hub.frames_fed, hub.total_dropped)

    if stats.upstream_frames == 0:
        logger.error("FAIL: upstream не дал ни одного фрейма — проверь upstream.url, "
                     "mountpoint, NTRIP_UPSTREAM_PASSWORD.")
        return 2
    if stats.downstream_frames == 0:
        logger.error("FAIL: control не получил ни одного фрейма от своего каста.")
        return 3

    ratio = stats.downstream_frames / max(stats.upstream_frames, 1)
    if ratio < 0.5:
        logger.warning("Downstream/upstream = %.0f%% — возможна проблема релая.", ratio * 100)
        return 4
    logger.info("OK: relay работает, downstream/upstream = %.0f%%", ratio * 100)
    return 0


def main() -> int:
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "--config", type=Path, default=Path("config.toml"),
        help="Путь к config.toml (default: ./config.toml)",
    )
    p.add_argument(
        "--duration", type=float, default=60.0,
        help="Длительность прогона в секундах (default: 60)",
    )
    args = p.parse_args()
    return asyncio.run(amain(config_path=args.config, duration=args.duration))


if __name__ == "__main__":
    sys.exit(main())
