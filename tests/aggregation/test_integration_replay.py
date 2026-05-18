"""Интеграционный прогон агрегатора + writer на лабораторных данных.

Поднимает NmeaReplayServer на session_a_rover2_rtk.nmea, подключается к
нему NmeaTcpClient, прогоняет сообщения через EpochAggregator,
эпохи пишет EpochBatchWriter в реальный EpochRepository поверх тестовой
базы (изоляция — откат транзакций).

Темп воспроизведения поднят до 200 Гц, чтобы тест шёл секунды, а не минуты.
"""

from __future__ import annotations

import asyncio
import os
import random
from datetime import UTC, datetime
from pathlib import Path

import pytest

from ntrip_accuracy_monitor.application.aggregation import (
    EpochAggregator,
    EpochBatchWriter,
)
from ntrip_accuracy_monitor.domain.solution_mode import SolutionMode
from ntrip_accuracy_monitor.persistence.epoch_repository import EpochRepository
from ntrip_accuracy_monitor.persistence.session_repository import (
    SessionRepository,
)
from ntrip_accuracy_monitor.protocols.nmea.messages import GgaRecord
from ntrip_accuracy_monitor.tools.replay.nmea_replay_server import (
    NmeaReplayServer,
)

from ntrip_accuracy_monitor.protocols.backoff import BackoffPolicy
from ntrip_accuracy_monitor.protocols.nmea.transport import NmeaTcpClient


def _find_repo_root(start: Path) -> Path:
    for parent in [start, *start.parents]:
        if (parent / "pyproject.toml").exists():
            return parent
    raise RuntimeError(f"pyproject.toml не найден выше {start}")


_REPO_ROOT = _find_repo_root(Path(__file__).resolve())
_CAPTURE_PATH = (
    _REPO_ROOT
    / "captures"
    / "lab_20260514_135834"
    / "session_a_rover2_rtk.nmea"
)
_EXPECTED_EPOCHS = 1264

pytestmark = [
    pytest.mark.skipif(
        not _CAPTURE_PATH.exists(),
        reason=f"нет файла капчи: {_CAPTURE_PATH}",
    ),
    pytest.mark.skipif(
        "PG_PASSWORD" not in os.environ,
        reason="не задана PG_PASSWORD — интеграционный тест пропущен",
    ),
]


async def _drain_until_done(
    client: NmeaTcpClient,
    aggregator: EpochAggregator,
    expected_gga: int,
    *,
    initial_timeout_s: float = 30.0,
    tail_timeout_s: float = 0.5,
) -> int:
    """Качать NMEA-сообщения из клиента в агрегатор.

    Пока GGA-счётчик меньше expected_gga — каждое сообщение ждётся с
    initial_timeout_s. После порога — с tail_timeout_s (хватит на GST
    последней эпохи; короче, чем initial_delay_s у BackoffPolicy, поэтому
    реконнект ещё не успеет открыть второй проход файла).
    """
    gga_count = 0
    it = aiter(client)
    while True:
        per_message_timeout = (
            initial_timeout_s if gga_count < expected_gga else tail_timeout_s
        )
        try:
            record = await asyncio.wait_for(
                anext(it), timeout=per_message_timeout
            )
        except (TimeoutError, StopAsyncIteration):
            break
        await aggregator.consume(record)
        if isinstance(record, GgaRecord):
            gga_count += 1
    return gga_count


@pytest.mark.asyncio
async def test_rtk_capture_lands_in_db(
    epoch_repository: EpochRepository,
    session_repository: SessionRepository,
    unused_tcp_port: int,
) -> None:
    """1264 RTK-эпохи с σ из GST доезжают до таблицы epochs."""
    port = unused_tcp_port
    server = NmeaReplayServer(
        nmea_path=_CAPTURE_PATH,
        host="127.0.0.1",
        port=port,
        loop_indefinitely=False,
        epoch_rate_hz=200.0,
    )
    await server.start()

    session_id = await session_repository.start(
        description="integration test RTK capture",
    )

    writer = EpochBatchWriter(
        epoch_repository,
        session_id_provider=lambda: session_id,
        flush_interval_s=0.25,
        max_buffer_size=200,
    )
    aggregator = EpochAggregator("rover_rtk", writer.submit)
    flusher = asyncio.create_task(writer.run_background_flusher())

    backoff = BackoffPolicy(initial_delay_s=1.0, max_delay_s=5.0)
    client = NmeaTcpClient(
        stream_id="rover_rtk",
        host="127.0.0.1",
        port=port,
        connect_timeout_s=5.0,
        stall_timeout_s=10.0,
        backoff=backoff,
        rng=random.Random(42),
    )

    gga_seen = 0
    try:
        # Страховка от зависания: при любом сбое тест выйдет через 60 с.
        async with asyncio.timeout(60):
            async with client:
                it = aiter(client)
                while True:
                    # Активная фаза — большой таймаут; после ожидаемых
                    # 1264 GGA — короткий, чтобы быстро выйти на хвосте.
                    timeout_s = 5.0 if gga_seen < _EXPECTED_EPOCHS else 0.2
                    try:
                        record = await asyncio.wait_for(
                            anext(it), timeout=timeout_s
                        )
                    except (TimeoutError, StopAsyncIteration):
                        break
                    if isinstance(record, GgaRecord):
                        # «Лишний» GGA — признак reconnect-цикла:
                        # выходим, его агрегатору не отдаём.
                        if gga_seen >= _EXPECTED_EPOCHS:
                            break
                        gga_seen += 1
                    await aggregator.consume(record)
            await aggregator.flush_pending()
    finally:
        await server.stop()
        writer.stop()
        try:
            await asyncio.wait_for(flusher, timeout=5.0)
        except TimeoutError:
            flusher.cancel()
        await writer.flush()

    assert gga_seen == _EXPECTED_EPOCHS, (
        f"клиент получил {gga_seen} GGA, ожидалось {_EXPECTED_EPOCHS}"
    )

    counts = await epoch_repository.count_by_solution_mode(session_id)
    assert counts.get(SolutionMode.RTK_FIXED, 0) == _EXPECTED_EPOCHS
    assert sum(counts.values()) == _EXPECTED_EPOCHS

    epochs = await epoch_repository.query_by_time_range(
        session_id=session_id,
        stream_id="rover_rtk",
        start=datetime(2000, 1, 1, tzinfo=UTC),
        end=datetime(3000, 1, 1, tzinfo=UTC),
    )
    assert len(epochs) == _EXPECTED_EPOCHS
    assert all(e.sigma_east_m is not None for e in epochs)
    assert all(e.sigma_north_m is not None for e in epochs)
    assert all(e.sigma_up_m is not None for e in epochs)

    assert aggregator.dropped_no_position == 0
    assert aggregator.dropped_invalid_format == 0
    assert writer.dropped_no_session == 0
    assert writer.dropped_db_unavailable == 0
