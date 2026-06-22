"""Управляемое инжектирование возраста поправок на плече ровера (Part B).

Работает на транспортном уровне над непрозрачными байтами RTCM:
содержимое поправок не разбирается, Z-count / временны́е метки /
контрольные суммы не переписываются. Подписчик кастера дренирует свою
очередь RtcmHub немедленно (буфер задержки локальный), поэтому
backpressure на Hub и на других подписчиков не создаётся.

Режимы (InjectionPlan.mode):
  passthrough — отдать роверу как есть (нулевая задержка);
  delay       — непрерывный сдвиг: кусок удерживается, пока его возраст
                не достигнет D(t); D(t) задаётся серией сегментов
                (duration_s, delay_s), при loop=True серия повторяется;
  dropout     — периодическое прерывание: в окне off роверу не уходит
                ничего (поправки устаревают), в окне on — проходит.

D(t) ступенчатая. При росте D лаг нарастает плавно (новые куски ждут
дольше), при падении буфер сливается быстрее (накопленные куски сразу
проходят) — дифференциальное решение остаётся непрерывным, без
артефактов повторной сходимости RTK, характерных для dropout.
"""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Final, Literal, assert_never
import logging
import time

logger: Final = logging.getLogger(__name__)

_SCHEDULE_TICK_S: Final[float] = 0.1
"""Период переоценки D(t) во сне delay-цикла (с)."""

_MIN_SLEEP_S: Final[float] = 0.001
"""Нижний порог сна, чтобы не уходить в busy-loop при близком релизе."""

type ChunkSender = Callable[[bytes], Awaitable[None]]
"""Отправка одного куска роверу (write+drain). Бросает при разрыве."""


def _elapsed_from(start: float) -> Callable[[], float]:
    """Часы «время сессии» = monotonic от точки start (с).

    Фолбэк, когда вызывающий не подал часы сессии: сохраняет прежнее
    поведение (отсчёт от старта данного соединения).
    """

    def _now() -> float:
        return time.monotonic() - start

    return _now


@dataclass(frozen=True, slots=True)
class InjectionPlan:
    """Транспортный план инжектирования (protocols-local, без Pydantic).

    Строится из CorrectionInjectionConfig.to_plan() на границе lifecycle.
    """

    mode: Literal["passthrough", "delay", "dropout"] = "passthrough"
    schedule: tuple[tuple[float, float], ...] = ()
    """Для mode=delay: серия (duration_s, delay_s)."""
    loop: bool = False
    dropout_on_s: float = 60.0
    dropout_off_s: float = 10.0


class _DelaySchedule:
    """Ступенчатая D(t) по серии сегментов (duration_s, delay_s)."""

    def __init__(
        self, segments: Sequence[tuple[float, float]], *, loop: bool,
    ) -> None:
        self._segments: list[tuple[float, float]] = [
            (float(dur), float(delay)) for dur, delay in segments
        ]
        self._loop = loop
        self._total = sum(dur for dur, _ in self._segments)

    def delay_at(self, elapsed_s: float) -> float:
        """Задержка D для прошедшего времени elapsed_s от старта потока."""
        if not self._segments:
            return 0.0
        t = elapsed_s
        if self._loop and self._total > 0.0:
            t %= self._total
        acc = 0.0
        for dur, delay in self._segments:
            acc += dur
            if t < acc:
                return delay
        # Серия пройдена (loop=False): держим задержку последнего сегмента.
        return self._segments[-1][1]


class _DropoutSchedule:
    """Периодический цикл on/off для прерывания потока."""

    def __init__(self, *, on_s: float, off_s: float) -> None:
        self._on = on_s
        self._period = on_s + off_s

    def is_open(self, elapsed_s: float) -> bool:
        """True — поток роверу проходит; False — окно off (молчим)."""
        if self._period <= 0.0:
            return True
        return (elapsed_s % self._period) < self._on


async def run_injection(
    queue: asyncio.Queue[bytes | None],
    send: ChunkSender,
    plan: InjectionPlan,
    *,
    now_session: Callable[[], float] | None = None,
) -> None:
    """Прокачать поток RtcmHub роверу согласно плану инжектирования.

    now_session — часы «время сессии» (с) от устойчивого старта сеанса,
    общие для всех подключений. Переподключение ровера НЕ перезапускает
    D(t)/цикл dropout. None — фолбэк на отсчёт от старта соединения
    (прежнее поведение; для прямых вызовов и тестов).

    Завершается на sentinel-None из очереди. В режиме delay перед выходом
    досылает удержанный хвост по расписанию. Исключения send пробрасываются.
    """
    clock: Callable[[], float] = (
        now_session if now_session is not None
        else _elapsed_from(time.monotonic())
    )
    match plan.mode:
        case "passthrough":
            await _run_passthrough(queue, send)
        case "delay":
            await _run_delay(
                queue, send, _DelaySchedule(plan.schedule, loop=plan.loop),
                clock,
            )
        case "dropout":
            await _run_dropout(
                queue, send,
                _DropoutSchedule(
                    on_s=plan.dropout_on_s, off_s=plan.dropout_off_s,
                ),
                clock,
            )
        case _ as unreachable:
            assert_never(unreachable)


async def _run_passthrough(
    queue: asyncio.Queue[bytes | None], send: ChunkSender,
) -> None:
    while True:
        item = await queue.get()
        if item is None:
            return
        await send(item)


async def _run_dropout(
    queue: asyncio.Queue[bytes | None],
    send: ChunkSender,
    schedule: _DropoutSchedule,
    now_session: Callable[[], float],
) -> None:
    last_open: bool | None = None
    while True:
        item = await queue.get()
        if item is None:
            return
        is_open = schedule.is_open(now_session())
        if is_open != last_open:
            logger.info(
                "Инжектирование dropout → %s (t сессии=%.0f с)",
                "пропуск" if is_open else "молчание", now_session(),
            )
            last_open = is_open
        if is_open:
            await send(item)
        # Иначе окно off: кусок отбрасывается, ровер поправок не получает.


async def _run_delay(
    queue: asyncio.Queue[bytes | None],
    send: ChunkSender,
    schedule: _DelaySchedule,
    now_session: Callable[[], float],
) -> None:
    buffer: deque[tuple[float, bytes]] = deque()
    last_logged_delay: float | None = None
    ended = False
    while True:
        target = schedule.delay_at(now_session())
        if target != last_logged_delay:
            logger.info(
                "Инжектирование delay → D=%.1f с (t сессии=%.0f с)",
                target, now_session(),
            )
            last_logged_delay = target
        now = time.monotonic()
        # Выпустить всё, чей возраст достиг текущей D(t).
        while buffer and (now - buffer[0][0]) >= target:
            _, chunk = buffer.popleft()
            await send(chunk)
            now = time.monotonic()
            target = schedule.delay_at(now_session())
        if ended and not buffer:
            return
        # Немедленно вычерпать очередь в локальный буфер (без backpressure).
        if not ended:
            while True:
                try:
                    item = queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
                if item is None:
                    ended = True
                    break
                buffer.append((time.monotonic(), item))
        # Поспать до ближайшего релиза либо до тика переоценки D(t).
        if buffer:
            due_in = target - (time.monotonic() - buffer[0][0])
            sleep_s = min(max(due_in, _MIN_SLEEP_S), _SCHEDULE_TICK_S)
        else:
            sleep_s = _SCHEDULE_TICK_S
        await asyncio.sleep(sleep_s)
