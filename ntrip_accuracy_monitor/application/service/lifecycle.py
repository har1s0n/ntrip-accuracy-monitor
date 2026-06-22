"""Главный оркестратор: один экземпляр на один сеанс наблюдений.

Жизненный цикл (метод ``run()``):
  1. Открыть сеанс в БД (``SessionRepository.start``).
  2. Установить обработчики SIGINT/SIGTERM через
     ``loop.add_signal_handler``
  3. Сконструировать компоненты: RtcmHub, RtcmAuditWriter,
     RoverGgaProvider (если задан gga_source_receiver_id),
     FileRtcmSink (если captures.enabled И upstream_ntrip.enabled),
     EpochAggregator+EpochBatchWriter для каждого ровера.
  4. Запустить ``asyncio.TaskGroup`` со всеми задачами:
       - подписчики RtcmHub: audit consume + audit flusher + file sink;
       - мост NTRIP → RtcmHub (если включён upstream);
       - для каждого ровера: NMEA bridge + epoch writer flusher.
  5. Дождаться остановки: внешняя отмена через сигнал, либо падение
     одной из задач.
  6. В ``finally`` — финальный сброс буферов, закрытие файла капчуры,
     закрытие сеанса (``SessionRepository.end``).

Соглашение по причинам завершения:
  - чистая отмена (только CancelledError в группе) → ``signal``;
  - смешанная или ошибочная группа → ``error`` + перевыброс;
  - штатный выход из ``TaskGroup`` (на практике не случается, задачи
    долгоживущие) → ``normal``.

``session_repo.end()`` в finally защищён от собственных падений: если
он упал (например, transient DB error), пишем в лог и НЕ перевыбрасываем —
иначе замаскируем оригинальное исключение из TaskGroup.

Повторный вызов ``run()`` запрещён: один экземпляр = один сеанс.
"""

from __future__ import annotations

import asyncio
import logging
import signal
import time
from typing import Any, Final

import asyncpg
from pydantic import AnyUrl
from urllib.parse import urlparse

from ntrip_accuracy_monitor.application.aggregation.epoch_aggregator import (
    EpochAggregator,
)
from ntrip_accuracy_monitor.application.aggregation.epoch_writer import (
    EpochBatchWriter,
)
from ntrip_accuracy_monitor.application.audit.rtcm_audit_writer import (
    RtcmAuditWriter,
)
from ntrip_accuracy_monitor.application.config import (
    AppConfig,
    BackoffConfig,
    NmeaReceiverConfig,
)
from ntrip_accuracy_monitor.application.service.file_rtcm_sink import (
    FileRtcmSink,
)
from ntrip_accuracy_monitor.persistence.epoch_repository import EpochRepository
from ntrip_accuracy_monitor.persistence.rtcm_repository import RtcmRepository
from ntrip_accuracy_monitor.persistence.session_repository import (
    SessionRepository,
    TerminationReason,
)
from ntrip_accuracy_monitor.protocols.nmea.transport import NmeaTcpClient
from ntrip_accuracy_monitor.protocols.ntrip._hub import RtcmHub
from ntrip_accuracy_monitor.protocols.ntrip._rover_gga import RoverGgaProvider
from ntrip_accuracy_monitor.protocols.ntrip.transport import NtripClient
from ntrip_accuracy_monitor.protocols.rtcm.adapter import RtcmAdapter

from ntrip_accuracy_monitor.application.service.metrics_service import (
    MetricsService,
)
from ntrip_accuracy_monitor.persistence.age_bin_metrics_repository import (
    AgeBinMetricsRepository,
)
from ntrip_accuracy_monitor.persistence.metrics_repository import (
    MetricsRepository,
)
from ntrip_accuracy_monitor.protocols.ntrip.caster import NtripCasterServer

logger: Final = logging.getLogger(__name__)

# ---- константы NTRIP ----
_NTRIP_DEFAULT_PORT: Final[int] = 2101
_NTRIPS_DEFAULT_PORT: Final[int] = 443

# ---- NMEA: дефолты, пока нет per-receiver конфига (долг #5) ----
_NMEA_CONNECT_TIMEOUT_S: Final[float] = 5.0
_NMEA_STALL_TIMEOUT_S: Final[float] = 5.0

# Подписчики Hub держим в одном кортеже для удобства итерации.
type _RoverComponents = tuple[
    NmeaReceiverConfig, EpochAggregator, EpochBatchWriter,
]


class SessionLifecycle:
    """Главный оркестратор сеанса. Один экземпляр = один прогон ``run()``."""

    def __init__(self, *, config: AppConfig, pool: asyncpg.Pool) -> None:
        self._config: Final = config
        self._pool: Final = pool

        # Репозитории. Pool как Executor — см. persistence/_executor.py.
        self._session_repo: Final = SessionRepository(pool)
        self._epoch_repo: Final = EpochRepository(pool)
        self._rtcm_repo: Final = RtcmRepository(pool)

        # Состояние сеанса.
        self._session_id: int | None = None
        self._session_start: float = 0.0
        self._run_called: bool = False

        # Компоненты, инициализируются в _run_tasks.
        self._rover_components: list[_RoverComponents] = []
        self._rtcm_audit: RtcmAuditWriter | None = None
        self._file_sink: FileRtcmSink | None = None
        self._gga_provider: RoverGgaProvider | None = None
        self._caster: NtripCasterServer | None = None

        self._hub: Final = RtcmHub(
            subscriber_queue_size=config.local_caster.subscriber_queue_size,
        )

        self._metrics_service: Final = MetricsService(
            self._session_repo,
            self._epoch_repo,
            executor=pool,
            metrics_repository=MetricsRepository(pool),
            age_bin_metrics_repository=AgeBinMetricsRepository(pool),
        )

    # ---------------------------- public API -----------------------------
    @property
    def session_id(self) -> int | None:
        """``session_id`` активного сеанса. None до start() и после end()."""
        return self._session_id

    @property
    def rtcm_hub(self) -> RtcmHub:
        """RtcmHub оркестратора.

        Публичный доступ — для тестов, которым нужно подавать байты
        прямо в Hub в обход NtripClient. В рабочем коде наружу не используется.
        """
        return self._hub

    async def run(self) -> None:
        """Главный цикл сеанса. См. module docstring."""
        if self._run_called:
            raise RuntimeError("SessionLifecycle.run() уже вызывался")
        self._run_called = True

        main_task = asyncio.current_task()
        self._install_signal_handlers(main_task)

        reason: TerminationReason = "normal"
        try:
            self._session_start = time.monotonic()
            self._session_id = await self._session_repo.start(
                description=self._build_description(),
                reference_antenna=self._build_reference_antenna(),
                config_snapshot=self._build_config_snapshot(),
            )
            logger.info("Сеанс %d открыт в БД", self._session_id)
            await self._run_tasks()
        except* asyncio.CancelledError:
            logger.info(
                "Сеанс %s отменён сигналом", self._session_id,
            )
            reason = "signal"
        except* BaseException as eg:
            # Любая не-Cancelled группа = ошибка. Перевыбрасываем
            # с исходным составом исключений.
            logger.error(
                "Сеанс %s упал: %d суб-исключение(й)",
                self._session_id, len(eg.exceptions),
            )
            reason = "error"
            raise
        finally:
            await self._shutdown(reason)

    # ---------------------------- TaskGroup ------------------------------
    async def _run_tasks(self) -> None:
        """Сконструировать компоненты и запустить TaskGroup со всеми задачами."""
        # 1. Без I/O: GGA-провайдер, агрегаторы/writer'ы эпох, RTCM audit.
        if self._is_gga_uplink_enabled():
            self._gga_provider = RoverGgaProvider()
            logger.info(
                "GGA-uplink включён: источник = %s",
                self._config.upstream_ntrip.gga_source_receiver_id,
            )

        self._rover_components = self._build_rover_components()
        if not self._rover_components:
            logger.warning(
                "В конфиге нет приёмников с ролью ровера — "
                "эпохи писаться не будут",
            )

        self._rtcm_audit = RtcmAuditWriter(
            adapter=RtcmAdapter(),
            repository=self._rtcm_repo,
            session_id_provider=lambda: self._session_id,
        )

        # 2. I/O: открыть файл. Возможный OSError пробрасывается
        # вверх — будет пойман except* BaseException в run().
        self._file_sink = await self._maybe_open_file_sink()

        self._caster = await self._maybe_start_caster()

        # 3. TaskGroup. Тут уже не делаем «отложенной» инициализации:
        # все компоненты готовы.
        async with asyncio.TaskGroup() as tg:
            # ---- подписчики RtcmHub (готовы до старта моста NTRIP) ----
            tg.create_task(
                self._consume_audit(self._rtcm_audit),
                name="rtcm-audit-consumer",
            )
            tg.create_task(
                self._rtcm_audit.run_background_flusher(),
                name="rtcm-audit-flusher",
            )
            if self._file_sink is not None:
                tg.create_task(
                    self._consume_file_sink(self._file_sink),
                    name="file-rtcm-sink",
                )

            # ---- мост NTRIP → Hub ----
            if self._config.upstream_ntrip.enabled:
                tg.create_task(
                    self._run_ntrip_bridge(self._gga_provider),
                    name="ntrip-bridge",
                )

            # ---- роверы: NMEA-bridge + epoch flusher ----
            gga_source_id = self._config.upstream_ntrip.gga_source_receiver_id
            for cfg, aggregator, writer in self._rover_components:
                gga_for_this = (
                    self._gga_provider
                    if cfg.receiver_id == gga_source_id
                    else None
                )
                tg.create_task(
                    self._run_nmea_bridge(cfg, aggregator, gga_for_this),
                    name=f"nmea-bridge-{cfg.receiver_id}",
                )
                tg.create_task(
                    writer.run_background_flusher(),
                    name=f"epoch-writer-{cfg.receiver_id}",
                )

            if self._rover_components:
                tg.create_task(
                    self._run_metrics_refresher(),
                    name="metrics-refresher",
                )

    # ---------------------------- task bodies ----------------------------
    async def _consume_audit(self, audit: RtcmAuditWriter) -> None:
        async with self._hub.subscribe() as queue:
            await audit.consume_hub(queue)

    async def _consume_file_sink(self, sink: FileRtcmSink) -> None:
        async with self._hub.subscribe() as queue:
            await sink.consume_hub(queue)

    async def _run_ntrip_bridge(
        self,
        gga_provider: RoverGgaProvider | None,
    ) -> None:
        """Перекладывает сырой поток RTCM из NtripClient в RtcmHub."""
        cfg = self._config.upstream_ntrip
        assert cfg.enabled, "_run_ntrip_bridge при upstream_ntrip.enabled=False"
        assert cfg.url is not None and cfg.mountpoint is not None
        host, port, use_https = _parse_ntrip_url(cfg.url)

        password = cfg.password.get_secret_value() if cfg.password else None
        async with NtripClient(
            stream_id=cfg.mountpoint,
            caster_host=host,
            caster_port=port,
            use_https=use_https,
            mountpoint=cfg.mountpoint,
            username=cfg.user,
            password=password,
            ntrip_version=cfg.ntrip_version,
            connect_timeout_s=cfg.connect_timeout_s,
            stall_timeout_s=cfg.stall_timeout_s,
            reconnect_backoff=cfg.backoff.to_policy(),
            queue_max_size=cfg.queue_max_size,
            gga_provider=(
                gga_provider.provide if gga_provider is not None else None
            ),
            gga_interval_s=cfg.gga_interval_s,
            raw=True,  # ретранслируем весь поток, включая RTCM 2.x (type 41)
        ) as client:
            async for chunk in client:
                self._hub.feed(chunk)

    async def _run_nmea_bridge(
        self,
        receiver: NmeaReceiverConfig,
        aggregator: EpochAggregator,
        gga_provider: RoverGgaProvider | None,
    ) -> None:
        """Читает NMEA из приёмника, кормит агрегатор (и, опционально, GGA-кэш)."""
        backoff = BackoffConfig().to_policy()
        async with NmeaTcpClient(
            stream_id=receiver.receiver_id,
            host=receiver.host,
            port=receiver.port,
            connect_timeout_s=_NMEA_CONNECT_TIMEOUT_S,
            stall_timeout_s=_NMEA_STALL_TIMEOUT_S,
            backoff=backoff,
        ) as client:
            async for record in client:
                await aggregator.consume(record)
                if gga_provider is not None:
                    await gga_provider.consume(record)

    async def _maybe_start_caster(self) -> NtripCasterServer | None:
        """Поднять локальный NTRIP-кастер поверх RtcmHub (раздача роверам).

        Возвращает None при upstream_ntrip.enabled=False: без апстрима в
        Hub не поступает RTCM — раздавать нечего, а слушающий порт с
        пустым потоком только путает ровер.
        """
        if not self._config.upstream_ntrip.enabled:
            logger.info(
                "Локальный кастер не поднят: upstream_ntrip.enabled=false "
                "(нет источника RTCM для раздачи)",
            )
            return None
        cfg = self._config.local_caster
        caster = NtripCasterServer(
            host=cfg.host,
            port=cfg.port,
            mountpoint=cfg.mountpoint,
            hub=self._hub,
            username=cfg.username,
            password=(
                cfg.password.get_secret_value()
                if cfg.password is not None else None
            ),
            sourcetable_country=cfg.sourcetable_country,
            handshake_timeout_s=cfg.handshake_timeout_s,
            injection=cfg.injection.to_plan(),
            now_session=lambda: time.monotonic() - self._session_start,
        )
        try:
            await caster.start()
        except BaseException:
            await caster.aclose()
            raise
        logger.info(
            "Локальный кастер поднят: %s:%d/%s (auth=%s) — ровер подключай сюда",
            cfg.host, cfg.port, cfg.mountpoint, "да" if cfg.username else "нет",
        )
        return caster

    async def _run_metrics_refresher(self) -> None:
        """Периодический пересчёт метрик сеанса (B3).

        Каждые ``metrics.refresh_interval_s`` пересчитывает и upsert'ит
        session_metrics / metrics_by_age для каждого ровера. Ошибки
        пересчёта логируются и НЕ пробрасываются — иначе сбой метрик
        обрушил бы TaskGroup и остановил ingest. CancelledError проходит
        насквозь (через sleep) для чистой остановки.
        """
        interval = self._config.metrics.refresh_interval_s
        logger.info("Рефрешер метрик запущен: интервал %.0f с", interval)
        while True:
            await asyncio.sleep(interval)
            await self._recompute_metrics(context="периодический")

    async def _recompute_metrics(self, *, context: str) -> None:
        """Пересчитать и записать метрики по всем роверам (persist=True).

        Каждый канал изолирован: исключение на одном не мешает другим и
        не выходит наружу (кроме CancelledError). Вызывается из рефрешера
        и из _shutdown (финальный пересчёт). compute_session_metrics —
        первым (создаёт строку session_metrics), затем age-bins.
        """
        sid = self._session_id
        if sid is None:
            return
        for cfg, _aggregator, _writer in self._rover_components:
            stream_id = cfg.receiver_id
            try:
                await self._metrics_service.compute_session_metrics(
                    sid, stream_id, persist=True,
                )
                await self._metrics_service.compute_session_age_bin_metrics(
                    sid, stream_id, persist=True,
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception(
                    "Пересчёт метрик (%s) для канала %s упал — пропускаю",
                    context, stream_id,
                )

    # ----------------------------- shutdown ------------------------------
    async def _shutdown(self, reason: TerminationReason) -> None:
        """Финальный порядок остановки (см. план в обсуждении).

        Каждый шаг защищён от собственных исключений: они только
        логируются, не пробрасываются — иначе finally в run() заменит
        оригинальное исключение из TaskGroup.
        """
        # 1. Последняя открытая эпоха каждого ровера → в буфер writer'а.
        for cfg, aggregator, _writer in self._rover_components:
            await _safe_call(
                aggregator.flush_pending(),
                what=f"agg.flush_pending({cfg.receiver_id})",
            )

        # 2. Финальный сброс буфера эпох в БД (под shield, пока есть session_id).
        for cfg, _aggregator, writer in self._rover_components:
            writer.stop()
            await _safe_call(
                asyncio.shield(writer.flush()),
                what=f"writer.flush({cfg.receiver_id})",
            )

        # 3. Финальный сброс RTCM-аудита (под shield, пока есть session_id).
        audit = self._rtcm_audit
        if audit is not None:
            audit.stop()
            await _safe_call(
                asyncio.shield(audit.flush()),
                what="rtcm_audit.flush",
            )

        # 4. Закрыть файл капчуры (от session_id не зависит).
        sink = self._file_sink
        self._file_sink = None
        if sink is not None:
            await _safe_call(sink.aclose(), what="file_sink.aclose")

        caster = self._caster
        self._caster = None
        if caster is not None:
            await _safe_call(caster.aclose(), what="caster.aclose")

        if reason in ("normal", "signal") and self._rover_components:
            await _safe_call(
                asyncio.shield(self._recompute_metrics(context="финальный")),
                what="metrics.final",
            )

        # 5. ОБНУЛЯЕМ session_id. После этой точки писать в БД через
        # session_id_provider бессмысленно — но writer'ы уже остановлены.
        sid = self._session_id
        self._session_id = None

        # 6. Закрыть сеанс. ВАЖНО: исключение тут НЕ перевыбрасываем,
        # иначе finally в run() заменит оригинальное исключение.
        if sid is not None:
            try:
                await asyncio.shield(self._session_repo.end(sid, reason))
                logger.info(
                    "Сеанс %d закрыт с termination_reason=%s", sid, reason,
                )
            except asyncio.CancelledError:
                logger.warning(
                    "session_repo.end(%d) прерван повторной отменой; "
                    "сеанс может остаться помечен как незавершённый",
                    sid,
                )
            except Exception:
                logger.exception(
                    "session_repo.end(%d, %s) упал; оригинальное "
                    "исключение (если есть) сохранено",
                    sid, reason,
                )

    # ----------------------------- helpers -------------------------------
    def _build_rover_components(self) -> list[_RoverComponents]:
        out: list[_RoverComponents] = []
        for cfg in self._config.nmea_receivers:
            if cfg.role not in ("rover_rtk", "rover_spp"):
                logger.info(
                    "Пропускаю NMEA-приёмник %s с ролью %s "
                    "(агрегатор/writer не создаются)",
                    cfg.receiver_id, cfg.role,
                )
                continue
            writer = EpochBatchWriter(
                repository=self._epoch_repo,
                session_id_provider=lambda: self._session_id,
            )
            aggregator = EpochAggregator(
                stream_id=cfg.receiver_id,
                on_epoch=writer.submit,
            )
            out.append((cfg, aggregator, writer))
        return out

    async def _maybe_open_file_sink(self) -> FileRtcmSink | None:
        if not (
            self._config.captures.enabled
            and self._config.upstream_ntrip.enabled
        ):
            return None
        mp = self._config.upstream_ntrip.mountpoint
        if not mp:
            logger.warning(
                "captures.enabled=true, но mountpoint не задан; "
                "капчура пропущена",
            )
            return None
        sid = self._session_id
        assert sid is not None, "session_id must be set before opening file sink"
        sink = FileRtcmSink(
            directory=self._config.captures.directory,
            session_id=sid,
            stream_id=mp,
        )
        try:
            await sink.aopen()
        except BaseException:
            await sink.aclose()
            raise
        return sink

    def _is_gga_uplink_enabled(self) -> bool:
        cfg = self._config.upstream_ntrip
        return cfg.enabled and cfg.gga_source_receiver_id is not None

    def _install_signal_handlers(
        self, main_task: asyncio.Task[None] | None,
    ) -> None:
        if main_task is None:
            logger.warning(
                "Не получилось определить current_task — "
                "обработчики сигналов не установлены",
            )
            return
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(
                    sig, self._on_signal, sig.name, main_task,
                )
            except NotImplementedError:
                logger.warning(
                    "loop.add_signal_handler не поддерживается на платформе "
                    "(сигнал=%s); чистая остановка по сигналу недоступна",
                    sig.name,
                )
                return

    def _on_signal(
        self, signal_name: str, main_task: asyncio.Task[None],
    ) -> None:
        logger.info("Получен сигнал %s — отмена главной задачи", signal_name)
        main_task.cancel()

    def _build_description(self) -> str:
        rovers = [
            r.receiver_id for r in self._config.nmea_receivers
            if r.role in ("rover_rtk", "rover_spp")
        ]
        return (
            f"automated session, rovers: "
            f"{','.join(rovers) if rovers else '(none)'}"
        )

    def _build_reference_antenna(self) -> dict[str, Any]:
        return self._config.reference_antenna.model_dump(mode="json")

    def _build_config_snapshot(self) -> dict[str, Any]:
        # mode='json' рендерит SecretStr как '**********', Path как str.
        return self._config.model_dump(mode="json")


# ============================ module-level helpers ============================
async def _safe_call(coro_or_future: Any, *, what: str) -> None:
    """Дождаться coroutine/future, проглотив все исключения с логом.

    Используется в _shutdown: исключения тут НЕ должны маскировать
    оригинальное исключение из TaskGroup. CancelledError (повторная
    отмена во время cleanup) тоже логируется и поглощается — без этого
    одна повторная отмена обрывает всю остановку.
    """
    try:
        await coro_or_future
    except asyncio.CancelledError:
        logger.warning(
            "shutdown step %s прерван повторной отменой — продолжаем", what,
        )
    except Exception:
        logger.exception("shutdown step %s упал", what)


def _parse_ntrip_url(url: AnyUrl) -> tuple[str, int, bool]:
    """Извлечь (host, port, use_https) из NTRIP-URL.

    Схема:
      - http://, ntrip://       → use_https=False, порт по умолчанию 2101;
      - https://, ntrips://     → use_https=True,  порт по умолчанию 443.

    Порт определяется так: если в URL он указан явно — берётся как есть,
    иначе подставляется NTRIP-дефолт.
    """
    host = url.host
    if not host:
        raise ValueError(f"upstream_ntrip.url не содержит host: {url}")
    scheme = (url.scheme or "").lower()
    use_https = scheme in ("https", "ntrips")

    explicit_port = urlparse(str(url)).port
    if explicit_port is not None:
        port = explicit_port
    else:
        port = _NTRIPS_DEFAULT_PORT if use_https else _NTRIP_DEFAULT_PORT
    return host, port, use_https
