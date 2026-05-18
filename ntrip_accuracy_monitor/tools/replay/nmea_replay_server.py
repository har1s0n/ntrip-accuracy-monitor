"""NmeaReplayServer — asyncio TCP-сервер для воспроизведения NMEA-захвата.

Имитирует RS3 в режиме «NMEA TCP-сервер»: подключающиеся клиенты получают
содержимое .nmea-файла, разбитое на эпохи и отправляемое с заданным
темпом (по умолчанию 1 Гц).

Параметры force_quality и position_jitter_m позволяют синтезировать
тестовые сценарии из одного и того же захвата:
- force_quality=2 поверх RTK-захвата → искусственный DGNSS-канал;
- force_quality=1 поверх любого → искусственный SPP;
- position_jitter_m > 0 → имитация менее точного режима.

Граница эпохи — следующий GGA. Сообщения до первого GGA отбрасываются.
Внутри эпохи все сообщения отправляются подряд, затем sleep до следующей
эпохи.

ИЗВЕСТНОЕ ОГРАНИЧЕНИЕ. position_jitter_m правит только координаты
в GGA.
Использовать только для отладки агрегатора, не для тестов
метрик точности.
"""

from __future__ import annotations

import asyncio
import logging
import math
import random
from contextlib import suppress
from pathlib import Path
from typing import Final

logger: Final = logging.getLogger(__name__)

# Приблизительная длина дуги в 1° широты в метрах (геоцентрическая модель).
# Для смещений в метрах при умеренных широтах ошибка < 0.5%, достаточно.
_METERS_PER_DEGREE: Final = 111_320.0


class NmeaReplayServer:
    """TCP-сервер, отдающий записанный NMEA-поток подключающимся клиентам.

    Args:
        nmea_path: путь к файлу с записанным NMEA-потоком.
        host: интерфейс для прослушивания.
        port: TCP-порт.
        force_quality: если задан, поле quality в каждом GGA переписывается
            на это значение, контрольная сумма пересчитывается.
        position_jitter_m: 1σ гауссова смещения позиции в плане (метры).
            Применяется независимо к восточной и северной составляющим.
        loop_indefinitely: после исчерпания файла начать с начала вместо
            закрытия соединения.
        epoch_rate_hz: темп выдачи эпох. По умолчанию 1.0 — как у живого
            ровера. Для unit-тестов имеет смысл задавать выше.
    """

    def __init__(
        self,
        nmea_path: Path,
        host: str,
        port: int,
        *,
        force_quality: int | None = None,
        position_jitter_m: float = 0.0,
        loop_indefinitely: bool = False,
        epoch_rate_hz: float = 1.0,
    ) -> None:
        if epoch_rate_hz <= 0.0:
            raise ValueError("epoch_rate_hz должен быть положительным")
        if position_jitter_m < 0.0:
            raise ValueError("position_jitter_m не может быть отрицательным")

        self._nmea_path: Final = nmea_path
        self._host: Final = host
        self._port: Final = port
        self._force_quality: Final = force_quality
        self._position_jitter_m: Final = position_jitter_m
        self._loop_indefinitely: Final = loop_indefinitely
        self._epoch_period_s: Final = 1.0 / epoch_rate_hz

        self._server: asyncio.Server | None = None
        self._epochs: tuple[tuple[bytes, ...], ...] = ()
        self._client_tasks: set[asyncio.Task[None]] = set()
        # счётчики
        self._clients_served = 0
        self._sentences_sent = 0

    @property
    def clients_served(self) -> int:
        return self._clients_served

    @property
    def sentences_sent(self) -> int:
        return self._sentences_sent

    @property
    def epoch_count(self) -> int:
        return len(self._epochs)

    async def start(self) -> None:
        """Загрузить файл, разобрать на эпохи, поднять TCP-сервер."""
        raw = await asyncio.to_thread(self._nmea_path.read_bytes)
        self._epochs = _parse_epochs(raw)
        logger.info(
            "NmeaReplayServer: загружено %d эпох из %s",
            len(self._epochs), self._nmea_path,
        )
        if not self._epochs:
            raise RuntimeError(
                f"в файле {self._nmea_path} не найдено ни одной GGA-эпохи",
            )

        self._server = await asyncio.start_server(
            self._handle_client, self._host, self._port,
        )
        sockets = self._server.sockets or ()
        bound = ", ".join(str(s.getsockname()) for s in sockets)
        logger.info("NmeaReplayServer слушает на %s", bound)

    async def stop(self) -> None:
        """Закрыть слушающий сокет и завершить все активные сессии."""
        if self._server is None:
            return
        self._server.close()
        with suppress(Exception):
            await self._server.wait_closed()
        self._server = None

        # активные клиенты завершатся сами на ConnectionResetError,
        # но дождаться корректно
        for task in list(self._client_tasks):
            task.cancel()
        if self._client_tasks:
            await asyncio.gather(*self._client_tasks, return_exceptions=True)
        self._client_tasks.clear()
        logger.info("NmeaReplayServer остановлен")

    async def _handle_client(
        self,
        _reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        peer = writer.get_extra_info("peername", "?")
        self._clients_served += 1
        logger.info("NmeaReplayServer: клиент %s подключился", peer)

        task = asyncio.current_task()
        if task is not None:
            self._client_tasks.add(task)
        try:
            await self._stream_to(writer)
        except (ConnectionResetError, BrokenPipeError):
            logger.info("NmeaReplayServer: клиент %s отвалился", peer)
        except asyncio.CancelledError:
            logger.info("NmeaReplayServer: клиент %s отменён по shutdown", peer)
            raise
        finally:
            with suppress(Exception):
                writer.close()
                await writer.wait_closed()
            if task is not None:
                self._client_tasks.discard(task)
            logger.info("NmeaReplayServer: клиент %s отключён", peer)

    async def _stream_to(self, writer: asyncio.StreamWriter) -> None:
        loop = asyncio.get_running_loop()
        while True:
            for epoch in self._epochs:
                t0 = loop.time()
                for sentence in epoch:
                    out = self._maybe_rewrite_gga(sentence)
                    writer.write(out)
                    self._sentences_sent += 1
                await writer.drain()
                elapsed = loop.time() - t0
                sleep_for = self._epoch_period_s - elapsed
                if sleep_for > 0:
                    await asyncio.sleep(sleep_for)
            if not self._loop_indefinitely:
                return

    def _maybe_rewrite_gga(self, raw: bytes) -> bytes:
        """Применить force_quality и/или jitter к GGA, иначе вернуть как есть."""
        if self._force_quality is None and self._position_jitter_m == 0.0:
            return raw
        if not _is_gga(raw):
            return raw
        return _rewrite_gga(
            raw,
            force_quality=self._force_quality,
            jitter_sigma_m=self._position_jitter_m,
        )


# ----------------------------------------------------------------------------
# Разбор файла на эпохи
# ----------------------------------------------------------------------------
def _parse_epochs(raw: bytes) -> tuple[tuple[bytes, ...], ...]:
    """Разбить .nmea-байты на эпохи, граница — каждый GGA.

    Строки без префикса $ игнорируются (битые куски на стыках захвата).
    """
    epochs: list[tuple[bytes, ...]] = []
    current: list[bytes] = []
    for line in raw.splitlines(keepends=True):
        if not line.startswith(b"$"):
            continue
        if _is_gga(line):
            if current:
                epochs.append(tuple(current))
            current = [line]
        elif current:
            current.append(line)
    if current:
        epochs.append(tuple(current))
    return tuple(epochs)


def _is_gga(line: bytes) -> bool:
    """Проверка по talker+id: $XXGGA в первых 7 байтах."""
    return len(line) >= 7 and line[3:6] == b"GGA"


# ----------------------------------------------------------------------------
# Точечное редактирование GGA + пересчёт XOR
# ----------------------------------------------------------------------------
def _rewrite_gga(
    raw: bytes,
    *,
    force_quality: int | None,
    jitter_sigma_m: float,
) -> bytes:
    """Переписать поля GGA, пересчитать контрольную сумму.

    Если в строке нет '*' или меньше 15 полей — вернуть как есть,
    не трогаем заведомо битые предложения.
    """
    if b"*" not in raw:
        return raw

    # Запомним окончание строки, чтобы воспроизвести при сборке
    if raw.endswith(b"\r\n"):
        eol = b"\r\n"
    elif raw.endswith(b"\n"):
        eol = b"\n"
    else:
        eol = b""

    star_idx = raw.index(b"*")
    body = raw[1:star_idx]  # без $ и *XX
    fields = body.split(b",")
    if len(fields) < 15:
        return raw

    changed = False

    if force_quality is not None:
        fields[6] = str(force_quality).encode("ascii")
        changed = True

    if jitter_sigma_m > 0.0:
        try:
            lat_dec = _nmea_to_decimal_deg(fields[2], fields[3])
            lon_dec = _nmea_to_decimal_deg(fields[4], fields[5])
        except ValueError:
            # координат нет (например, пока приёмник не зафиксировался) —
            # возвращаем строку без правок
            if not changed:
                return raw
        else:
            d_north_m = random.gauss(0.0, jitter_sigma_m)
            d_east_m = random.gauss(0.0, jitter_sigma_m)
            d_lat = d_north_m / _METERS_PER_DEGREE
            cos_lat = math.cos(math.radians(lat_dec))
            # защита от деления на ноль около полюсов
            d_lon = d_east_m / (_METERS_PER_DEGREE * cos_lat) if cos_lat else 0.0

            new_lat = lat_dec + d_lat
            new_lon = lon_dec + d_lon
            fields[2], fields[3] = _decimal_to_nmea(new_lat, is_lat=True)
            fields[4], fields[5] = _decimal_to_nmea(new_lon, is_lat=False)
            changed = True

    if not changed:
        return raw

    new_body = b",".join(fields)
    xor = 0
    for byte_val in new_body:
        xor ^= byte_val
    checksum = f"{xor:02X}".encode("ascii")
    return b"$" + new_body + b"*" + checksum + eol


def _nmea_to_decimal_deg(field: bytes, direction: bytes) -> float:
    """NMEA-форма DDMM.MMMM (+N/S/E/W) → десятичные градусы со знаком."""
    if not field or not direction:
        raise ValueError("пустое поле координаты")
    raw_val = float(field.decode("ascii"))
    degrees = int(raw_val // 100)
    minutes = raw_val - degrees * 100
    decimal = degrees + minutes / 60.0
    if direction in (b"S", b"W"):
        decimal = -decimal
    return decimal


def _decimal_to_nmea(decimal_deg: float, *, is_lat: bool) -> tuple[bytes, bytes]:
    """Десятичные градусы → NMEA-форма (поле, направление).

    Широта: DDMM.MMMMMM (2 знака градусов). Долгота: DDDMM.MMMMMM (3).
    Минуты с 6 знаками после точки — типичная точность RS3.
    """
    if is_lat:
        direction = b"N" if decimal_deg >= 0 else b"S"
    else:
        direction = b"E" if decimal_deg >= 0 else b"W"
    abs_deg = abs(decimal_deg)
    degrees = int(abs_deg)
    minutes = (abs_deg - degrees) * 60.0
    if is_lat:
        text = f"{degrees:02d}{minutes:09.6f}"
    else:
        text = f"{degrees:03d}{minutes:09.6f}"
    return text.encode("ascii"), direction
