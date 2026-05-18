"""Подготовленные данные для тестов агрегации."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

import pytest

from ntrip_accuracy_monitor.domain.position import GeodeticPosition
from ntrip_accuracy_monitor.domain.solution_mode import SolutionMode
from ntrip_accuracy_monitor.protocols.nmea.messages import (
    GgaRecord,
    GsaRecord,
    GstRecord,
    RmcRecord,
    ZdaRecord,
)
import pytest_asyncio

from ntrip_accuracy_monitor.persistence.epoch_repository import EpochRepository
from ntrip_accuracy_monitor.persistence.session_repository import (
    SessionRepository,
)

_DEFAULT_POSITION = GeodeticPosition(
    latitude_deg=55.123456,
    longitude_deg=37.654321,
    ellipsoidal_height_m=200.0,
)


@pytest.fixture
def default_position() -> GeodeticPosition:
    return _DEFAULT_POSITION


@pytest.fixture
def make_gga() -> Callable[..., GgaRecord]:
    def _make(
        *,
        time_utc: datetime,
        solution_mode: SolutionMode = SolutionMode.RTK_FIXED,
        position: GeodeticPosition | None = _DEFAULT_POSITION,
        satellites_used: int = 18,
        hdop: float | None = 0.8,
        age_of_corrections_s: float | None = 1.2,
        reference_station_id: int | None = 12345,
    ) -> GgaRecord:
        return GgaRecord(
            time_utc=time_utc,
            position=position,
            solution_mode=solution_mode,
            satellites_used=satellites_used,
            hdop=hdop,
            age_of_corrections_s=age_of_corrections_s,
            reference_station_id=reference_station_id,
        )

    return _make


@pytest.fixture
def make_gst() -> Callable[..., GstRecord]:
    def _make(
        *,
        time_utc: datetime,
        sigma_east_m: float = 0.015,
        sigma_north_m: float = 0.012,
        sigma_up_m: float = 0.025,
    ) -> GstRecord:
        return GstRecord(
            time_utc=time_utc,
            sigma_east_m=sigma_east_m,
            sigma_north_m=sigma_north_m,
            sigma_up_m=sigma_up_m,
        )

    return _make


@pytest.fixture
def make_gsa() -> Callable[..., GsaRecord]:
    def _make(
        *,
        pdop: float | None = 1.4,
        hdop: float | None = 0.7,
        vdop: float | None = 1.2,
        fix_type: int = 3,
        satellite_prns: tuple[int, ...] = (1, 3, 7, 11, 18, 22, 25),
    ) -> GsaRecord:
        return GsaRecord(
            pdop=pdop,
            hdop=hdop,
            vdop=vdop,
            fix_type=fix_type,
            satellite_prns=satellite_prns,
        )

    return _make


@pytest.fixture
def make_rmc() -> Callable[..., RmcRecord]:
    def _make(
        *,
        time_utc: datetime,
        is_valid: bool = True,
    ) -> RmcRecord:
        return RmcRecord(time_utc=time_utc, is_valid=is_valid)

    return _make


@pytest.fixture
def make_zda() -> Callable[..., ZdaRecord]:
    def _make(*, time_utc: datetime) -> ZdaRecord:
        return ZdaRecord(time_utc=time_utc)

    return _make


def utc(
    year: int = 2026,
    month: int = 5,
    day: int = 16,
    hour: int = 12,
    minute: int = 0,
    second: int = 0,
    microsecond: int = 0,
) -> datetime:
    """Сокращалка для построения TZ-aware UTC datetime в тестах."""
    return datetime(
        year, month, day, hour, minute, second, microsecond, tzinfo=UTC
    )


@pytest_asyncio.fixture
async def epoch_repository(db_conn) -> EpochRepository:  # type: ignore[no-untyped-def]
    """EpochRepository поверх db_conn (с откатом транзакции в финализаторе)."""
    return EpochRepository(db_conn)


@pytest_asyncio.fixture
async def session_repository(db_conn) -> SessionRepository:  # type: ignore[no-untyped-def]
    """SessionRepository поверх db_conn (с откатом транзакции в финализаторе)."""
    return SessionRepository(db_conn)
