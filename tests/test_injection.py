# tests/protocols/ntrip/test_injection.py — test_delay_reads_session_clock
import asyncio
import time

from ntrip_accuracy_monitor.protocols.ntrip.injection import (
    _DelaySchedule,
    _run_delay,
)

_CHUNK = b"\xd3\x00\x04test"


async def _release_latency(session_t: float) -> float:
    """Задержка доставки одного куска при фиксированном времени сессии (с).

    Очередь предзаполнена (кусок + sentinel); буфер пуст на входе —
    эмулирует свежее подключение ровера.
    """
    queue: asyncio.Queue[bytes | None] = asyncio.Queue()
    queue.put_nowait(_CHUNK)
    queue.put_nowait(None)
    schedule = _DelaySchedule([(10.0, 0.0), (10.0, 0.3)], loop=False)
    sent: list[tuple[float, bytes]] = []

    async def send(chunk: bytes) -> None:
        sent.append((time.monotonic(), chunk))

    t0 = time.monotonic()
    await asyncio.wait_for(
        _run_delay(queue, send, schedule, lambda: session_t),
        timeout=2.0,
    )
    assert [c for _, c in sent] == [_CHUNK]
    return sent[0][0] - t0


async def test_delay_reads_session_clock_not_connection_start() -> None:
    # Время сессии в сегменте D=0 → доставка немедленная.
    assert await _release_latency(2.0) < 0.2
    # Свежее подключение, но время сессии уже в сегменте D=0.3 → кусок
    # удерживается ~0.3 с. Привязка к старту соединения дала бы сегмент 0
    # (D=0) и немедленную доставку — этого быть не должно.
    assert await _release_latency(12.0) >= 0.25
