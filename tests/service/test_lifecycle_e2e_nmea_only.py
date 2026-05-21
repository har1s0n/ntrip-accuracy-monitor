"""Мини-e2e SessionLifecycle: один NMEA-канал, без upstream_ntrip и записи в файл.

Зависит от фикстур ``pool`` и ``pg_config`` из tests/conftest.py.
Условия пропуска:
  - PG_PASSWORD не задан (из фикстуры pg_config);
  - подготовленный NMEA-файл (session_a_rover2_rtk.nmea) отсутствует.
"""

from __future__ import annotations

import asyncio
import socket
from pathlib import Path
from typing import Final

import asyncpg
import pytest

from ntrip_accuracy_monitor.application.config import (
    AppConfig,
    CapturesConfig,
    LocalCasterConfig,
    NmeaReceiverConfig,
    PostgresConfig,
    ReferenceAntennaConfig,
    UpstreamNtripConfig,
)
from ntrip_accuracy_monitor.application.service.lifecycle import (
    SessionLifecycle,
)
from ntrip_accuracy_monitor.tools.replay.nmea_replay_server import (
    NmeaReplayServer,
)

_FIXTURE_NMEA: Final[Path] = (
    Path(__file__).resolve().parents[2]
    / "tests" / "fixtures" / "session_a_rover2_rtk.nmea"
)


def _free_tcp_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _make_config(*, nmea_port: int, pg: PostgresConfig) -> AppConfig:
    return AppConfig(
        log_level="INFO",
        postgres=pg,
        local_caster=LocalCasterConfig(mountpoint="TEST"),
        upstream_ntrip=UpstreamNtripConfig(enabled=False),
        nmea_receivers=[
            NmeaReceiverConfig(
                receiver_id="rover2",
                host="127.0.0.1",
                port=nmea_port,
                role="rover_rtk",
            ),
        ],
        reference_antenna=ReferenceAntennaConfig(
            latitude_deg=52.2,
            longitude_deg=21.0,
            ellipsoidal_height_m=120.0,
        ),
        captures=CapturesConfig(enabled=False),
    )


@pytest.mark.asyncio
async def test_lifecycle_nmea_only_signal_shutdown(
    pool: asyncpg.Pool,
    pg_config: PostgresConfig,
) -> None:
    if not _FIXTURE_NMEA.is_file():
        pytest.skip(f"подготовленный NMEA-файл отсутствует: {_FIXTURE_NMEA}")

    port = _free_tcp_port()
    server = NmeaReplayServer(
        nmea_path=_FIXTURE_NMEA,
        host="127.0.0.1",
        port=port,
        loop_indefinitely=True,
        epoch_rate_hz=10.0,  # ускоряем — иначе уйдёт на минуты
    )
    await server.start()

    sid: int | None = None
    try:
        config = _make_config(nmea_port=port, pg=pg_config)
        lifecycle = SessionLifecycle(config=config, pool=pool)
        run_task = asyncio.create_task(lifecycle.run(), name="lifecycle-run")

        # Дать оркестратору поднять задачи и собрать несколько эпох.
        await asyncio.sleep(2.0)
        sid = lifecycle.session_id
        assert sid is not None, "session_id должен быть установлен в start()"
        run_task.cancel()
        # CancelledError из TaskGroup поглощается в except* — run() вернётся.
        await run_task

        # Проверка БД на том же pool (отдельным соединением).
        async with pool.acquire() as conn:
            session_row = await conn.fetchrow(
                "SELECT ended_at, termination_reason FROM sessions "
                "WHERE session_id = $1",
                sid,
            )
            epoch_count = await conn.fetchval(
                "SELECT COUNT(*) FROM epochs WHERE session_id = $1", sid,
            )

        assert session_row is not None
        assert session_row["ended_at"] is not None
        assert session_row["termination_reason"] == "signal"
        assert epoch_count > 0, "ожидаем хотя бы одну записанную эпоху"
    finally:
        await server.stop()
