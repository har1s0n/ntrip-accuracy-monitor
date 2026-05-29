from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import pandas as pd
import streamlit as st

from ntrip_accuracy_monitor.gui._db import fetch_records

_log = logging.getLogger(__name__)

_SESSIONS_SQL = """
SELECT
    s.session_id,
    s.started_at,
    s.ended_at,
    s.description,
    s.termination_reason,
    (s.ended_at IS NULL) AS is_active,
    COALESCE((
        SELECT COUNT(*)::bigint
        FROM epochs
        WHERE session_id = s.session_id
    ), 0) AS epochs_count_total,
    COALESCE(ARRAY(
        SELECT DISTINCT stream_id
        FROM epochs
        WHERE session_id = s.session_id
        ORDER BY stream_id
    ), ARRAY[]::text[]) AS streams
FROM sessions s
ORDER BY s.started_at DESC
LIMIT $1;
"""


@st.cache_data(ttl=30)
def load_sessions(limit: int = 200) -> pd.DataFrame:
    """
    Возвращает DataFrame с колонками:
        session_id (int64),
        started_at (UTC tz-aware),
        ended_at   (UTC tz-aware | NaT),
        description (str), termination_reason (str | None),
        is_active (bool),                    -- ended_at IS NULL
        duration_s (float, секунды; для активных — от started_at до now UTC),
        epochs_count_total (int64),
        streams (list[str]).                 -- DISTINCT stream_id из epochs

    Таймстампы конвертируются в timezone-aware UTC прямо здесь.
    Кэшируется на 30 секунд.

    NB:
      - COUNT(*) FROM epochs WHERE session_id = $1 идёт по композитному btree
        (session_id, solution_mode, epoch_time) как index-only scan; на 520k
        строк ожидается < 50 ms. Если станет узким местом — денормализовать
        счётчик в sessions или материализовать.
      - SELECT DISTINCT stream_id — тот же индекс, 2-3 уникальных значения.
    """
    rows = fetch_records(_SESSIONS_SQL, limit)

    df = pd.DataFrame(
        [dict(r) for r in rows],
        columns=[
            "session_id",
            "started_at",
            "ended_at",
            "description",
            "termination_reason",
            "is_active",
            "epochs_count_total",
            "streams",
        ],
    )

    df["started_at"] = pd.to_datetime(df["started_at"], utc=True)
    df["ended_at"] = pd.to_datetime(df["ended_at"], utc=True)

    now_utc = pd.Timestamp.now(tz="UTC")
    end_or_now = df["ended_at"].fillna(now_utc)
    df["duration_s"] = (end_or_now - df["started_at"]).dt.total_seconds()

    df["session_id"] = df["session_id"].astype("int64")
    df["epochs_count_total"] = df["epochs_count_total"].astype("int64")
    df["is_active"] = df["is_active"].astype("bool")

    return df


@dataclass(frozen=True, slots=True)
class SessionHeader:
    session_id: int
    started_at: datetime  # tz-aware UTC
    ended_at: datetime | None  # None → активна
    description: str | None
    termination_reason: str | None


@dataclass(frozen=True, slots=True)
class RtcmStatus:
    last_received_at: datetime | None  # tz-aware UTC
    server_now: datetime  # tz-aware UTC, для расчёта stale
    msg_per_sec: float
    bytes_per_sec: float
    distinct_types: int


@dataclass(frozen=True, slots=True)
class LiveKpi:
    epochs_in_window: int  # rover_rtk + rover_spp
    rtk_fixed_share: float | None  # доля mode==4 у rover_rtk, [0..1]
    last_age_s: float | None  # rover_rtk, последняя эпоха
    last_sigma_h_m: float | None  # rover_rtk, последняя эпоха


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


_SESSION_HEADER_SQL = """\
SELECT session_id, started_at, ended_at, description, termination_reason
FROM sessions
WHERE session_id = $1
"""

_LAST_EPOCH_PER_STREAM_SQL = """\
SELECT DISTINCT ON (stream_id)
       stream_id, epoch_time, solution_mode, satellites_used, hdop,
       now() AS server_now
FROM epochs
WHERE session_id = $1
ORDER BY stream_id, epoch_time DESC
"""

_WINDOW_EPOCHS_SQL = """\
WITH bounds AS (
    SELECT max(epoch_time) AS t_max
    FROM epochs
    WHERE session_id = $1 AND stream_id IN ('rover_rtk', 'rover_spp')
)
SELECT e.epoch_time, e.stream_id, e.solution_mode,
       e.sigma_east_m, e.sigma_north_m, e.age_of_corrections_s
FROM epochs e, bounds b
WHERE e.session_id = $1
  AND e.stream_id IN ('rover_rtk', 'rover_spp')
  AND b.t_max IS NOT NULL
  AND e.epoch_time > b.t_max - interval '1 second' * $2
ORDER BY e.epoch_time
"""

_RTCM_STATUS_SQL = """\
WITH bounds AS (
    SELECT max(received_at) AS t_max
    FROM rtcm_messages
    WHERE session_id = $1
),
win AS (
    SELECT count(*) AS msg_count,
           COALESCE(sum(byte_length), 0)::bigint AS total_bytes,
           count(DISTINCT msg_type) AS distinct_types
    FROM rtcm_messages, bounds
    WHERE session_id = $1
      AND bounds.t_max IS NOT NULL
      AND received_at > bounds.t_max - interval '1 second' * $2
)
SELECT bounds.t_max AS last_received_at,
       now() AS server_now,
       win.msg_count, win.total_bytes, win.distinct_types
FROM bounds, win
"""

_RTCM_RATE_SQL = """\
WITH bounds AS (
    SELECT max(received_at) AS t_max
    FROM rtcm_messages
    WHERE session_id = $1
)
SELECT to_timestamp(floor(extract(epoch FROM received_at) / $3) * $3) AS bucket_time,
       msg_type,
       count(*)::double precision / $3 AS msg_per_sec
FROM rtcm_messages, bounds
WHERE session_id = $1
  AND bounds.t_max IS NOT NULL
  AND received_at > bounds.t_max - interval '1 second' * $2
GROUP BY bucket_time, msg_type
ORDER BY bucket_time, msg_type
"""


@st.cache_data(ttl=5)
def check_db_health() -> bool:
    try:
        fetch_records("SELECT 1")
    except Exception:  # health-probe: любая ошибка = БД недоступна
        _log.warning("DB health probe failed", exc_info=True)
        return False
    return True


@st.cache_data(ttl=2)
def load_session_header(session_id: int) -> SessionHeader | None:
    rows = fetch_records(_SESSION_HEADER_SQL, session_id)
    if not rows:
        return None
    r = rows[0]
    ended = r["ended_at"]
    return SessionHeader(
        session_id=int(r["session_id"]),
        started_at=_as_utc(r["started_at"]),
        ended_at=_as_utc(ended) if ended is not None else None,
        description=r["description"],
        termination_reason=r["termination_reason"],
    )


@st.cache_data(ttl=1)
def load_last_epoch_per_stream(session_id: int) -> pd.DataFrame:
    cols = ["stream_id", "epoch_time", "solution_mode",
            "satellites_used", "hdop", "server_now"]
    rows = fetch_records(_LAST_EPOCH_PER_STREAM_SQL, session_id)
    if not rows:
        return pd.DataFrame(columns=cols)
    df = pd.DataFrame([dict(r) for r in rows], columns=cols)
    df["epoch_time"] = pd.to_datetime(df["epoch_time"], utc=True)
    df["server_now"] = pd.to_datetime(df["server_now"], utc=True)
    return df


@st.cache_data(ttl=1)
def load_window_epochs(session_id: int, window_s: int) -> pd.DataFrame:
    cols = ["epoch_time", "stream_id", "solution_mode",
            "sigma_east_m", "sigma_north_m", "age_of_corrections_s"]
    rows = fetch_records(_WINDOW_EPOCHS_SQL, session_id, window_s)
    if not rows:
        return pd.DataFrame(columns=cols)
    df = pd.DataFrame([dict(r) for r in rows], columns=cols)
    df["epoch_time"] = pd.to_datetime(df["epoch_time"], utc=True)
    return df


@st.cache_data(ttl=1)
def load_rtcm_status(session_id: int, window_s: int) -> RtcmStatus | None:
    rows = fetch_records(_RTCM_STATUS_SQL, session_id, window_s)
    if not rows:
        return None
    r = rows[0]
    last = r["last_received_at"]
    span = float(window_s) if window_s else 1.0
    return RtcmStatus(
        last_received_at=_as_utc(last) if last is not None else None,
        server_now=_as_utc(r["server_now"]),
        msg_per_sec=float(r["msg_count"]) / span,
        bytes_per_sec=float(r["total_bytes"]) / span,
        distinct_types=int(r["distinct_types"]),
    )


@st.cache_data(ttl=2)
def load_rtcm_rate_timeseries(
    session_id: int, window_s: int, bucket_s: int = 5
) -> pd.DataFrame:
    cols = ["bucket_time", "msg_type", "msg_per_sec"]
    rows = fetch_records(_RTCM_RATE_SQL, session_id, window_s, bucket_s)
    if not rows:
        return pd.DataFrame(columns=cols)
    df = pd.DataFrame([dict(r) for r in rows], columns=cols)
    df["bucket_time"] = pd.to_datetime(df["bucket_time"], utc=True)
    return df


def compute_live_kpi(epoch_df: pd.DataFrame) -> LiveKpi:
    """KPI из загруженного окна (pandas, без обращения к БД). D1: без фильтра режима."""
    epochs_in_window = int(len(epoch_df))
    rtk = epoch_df[epoch_df["stream_id"] == "rover_rtk"]
    if rtk.empty:
        return LiveKpi(epochs_in_window, None, None, None)
    share = float((rtk["solution_mode"] == 4).mean())
    last = rtk.sort_values("epoch_time").iloc[-1]
    age = last["age_of_corrections_s"]
    se, sn = last["sigma_east_m"], last["sigma_north_m"]
    sigma_h = (
        float((float(se) ** 2 + float(sn) ** 2) ** 0.5)
        if pd.notna(se) and pd.notna(sn) else None
    )
    return LiveKpi(
        epochs_in_window=epochs_in_window,
        rtk_fixed_share=share,
        last_age_s=float(age) if pd.notna(age) else None,
        last_sigma_h_m=sigma_h,
    )


@dataclass(frozen=True, slots=True)
class SessionDetail:
    session_id: int
    started_at: datetime  # tz-aware UTC
    ended_at: datetime | None  # None → активна
    description: str | None
    termination_reason: str | None  # normal|signal|error|None
    reference_antenna: dict[str, Any] | None  # ключи: lat, lon, h, source
    config_snapshot: dict[str, Any] | None


_SESSION_DETAIL_SQL = """\
SELECT session_id, started_at, ended_at, description,
       termination_reason, reference_antenna, config_snapshot
FROM sessions
WHERE session_id = $1
"""

_SESSION_METRICS_SQL = """\
SELECT stream_id, solution_mode_filter, epochs_after_filter,
       hrms_m, vrms_m, two_drms_m, cep50_m, r95_m,
       error_3d_max_m, fixed_ratio
FROM session_metrics
WHERE session_id = $1
ORDER BY
  CASE stream_id
      WHEN 'base' THEN 0 WHEN 'rover_rtk' THEN 1
      WHEN 'rover_spp' THEN 2 ELSE 3 END,
  CASE solution_mode_filter
      WHEN 'DGNSS' THEN 0 WHEN 'RTK_FIXED' THEN 1
      WHEN 'RTK_FIXED_FLOAT' THEN 2 WHEN 'SPP' THEN 3 ELSE 4 END
"""

_METRICS_BY_AGE_SQL = """\
SELECT a.age_bin_start_s, a.age_bin_end_s, a.epochs_count,
       a.hrms_m, a.cep50_m, a.r95_m, a.is_significant
FROM metrics_by_age a
JOIN session_metrics m ON m.metrics_id = a.metrics_id
WHERE m.session_id = $1
  AND m.stream_id = $2
  AND m.solution_mode_filter = $3
ORDER BY a.age_bin_start_s
"""


@st.cache_data(ttl=300)
def load_session_detail(session_id: int) -> SessionDetail | None:
    rows = fetch_records(_SESSION_DETAIL_SQL, session_id)
    if not rows:
        return None
    r = rows[0]
    ended = r["ended_at"]
    return SessionDetail(
        session_id=int(r["session_id"]),
        started_at=_as_utc(r["started_at"]),
        ended_at=_as_utc(ended) if ended is not None else None,
        description=r["description"],
        termination_reason=r["termination_reason"],
        reference_antenna=r["reference_antenna"],  # dict | None (codec в _db.py)
        config_snapshot=r["config_snapshot"],  # dict | None
    )


@st.cache_data(ttl=300)
def load_session_metrics(session_id: int) -> pd.DataFrame:
    cols = ["stream_id", "solution_mode_filter", "epochs_after_filter",
            "hrms_m", "vrms_m", "two_drms_m", "cep50_m", "r95_m",
            "error_3d_max_m", "fixed_ratio"]
    rows = fetch_records(_SESSION_METRICS_SQL, session_id)
    if not rows:
        return pd.DataFrame(columns=cols)
    return pd.DataFrame([dict(r) for r in rows], columns=cols)


@st.cache_data(ttl=300)
def load_metrics_by_age(
    session_id: int, stream_id: str, solution_mode_filter: str
) -> pd.DataFrame:
    cols = ["age_bin_start_s", "age_bin_end_s", "epochs_count",
            "hrms_m", "cep50_m", "r95_m", "is_significant"]
    rows = fetch_records(_METRICS_BY_AGE_SQL, session_id, stream_id, solution_mode_filter)
    if not rows:
        return pd.DataFrame(columns=cols)
    df = pd.DataFrame([dict(r) for r in rows], columns=cols)
    df["age_mid_s"] = (df["age_bin_start_s"] + df["age_bin_end_s"]) / 2.0
    return df
