"""Наблюдение в реальном времени за выбранной сессией (view-only)."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Final

import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from streamlit_autorefresh import st_autorefresh

from ntrip_accuracy_monitor.gui._data import (
    LiveKpi,
    RtcmStatus,
    SessionHeader,
    check_db_health,
    compute_live_kpi,
    load_last_epoch_per_stream,
    load_rtcm_rate_timeseries,
    load_rtcm_status,
    load_session_header,
    load_sessions,
    load_window_epochs,
)
from ntrip_accuracy_monitor.gui._db import get_app_config
from ntrip_accuracy_monitor.gui._format import (
    AGE_WARN_S,
    apply_ops_theme,
    format_channel_status,
    format_duration,
    format_hms,
    format_solution_mode,
    format_utc,
    kpi_age_color,
    kpi_share_color,
    kpi_sigma_color,
    rtcm_type_label,
    solution_mode_color,
)
from ntrip_accuracy_monitor.gui._sidebar import render_session_selector

_STATUS_STREAMS: Final = ("base", "rover_rtk", "rover_spp")
_MODE_FILTER_ORDER: Final = (4, 5, 2, 1, 0)
_DEFAULT_MODES: Final = frozenset({4, 5, 2})
_RTCM_PALETTE: Final = ("#3DD6C4", "#3b82f6", "#8b5cf6", "#f59e0b", "#ef4444", "#7d8b99")
_STRIP_MUTED: Final = "#2a3a47"  # отфильтрованный режим на тёмном фоне
_SUB_COLOR: Final = "#8fa3b3"


# --- утилиты вёрстки --------------------------------------------------------
def _vspace(px: int = 10) -> None:
    st.markdown(f"<div style='height:{px}px'></div>", unsafe_allow_html=True)


def _soft_top(series_max: float | None, default_top: float) -> float:
    if series_max is None or series_max <= default_top:
        return default_top
    return float(series_max) * 1.1


# --- sidebar ----------------------------------------------------------------
def _render_pause_control() -> bool:
    with st.sidebar:
        st.divider()
        return st.checkbox("Пауза автообновления", value=False, key="live_paused")


def _render_mode_filter() -> frozenset[int]:
    with st.sidebar:
        st.divider()
        st.markdown("**Фильтр по режиму решения**")
        allowed: set[int] = set()
        for code in _MODE_FILTER_ORDER:
            if st.checkbox(format_solution_mode(code),
                           value=code in _DEFAULT_MODES, key=f"live_mode_{code}"):
                allowed.add(code)
    return frozenset(allowed)


def _render_sidebar_footer(window_s: int, auto_refresh_ms: int) -> None:
    with st.sidebar:
        st.divider()
        st.caption(f"Окно: {format_duration(window_s)}")
        st.caption(f"Обновление каждые {auto_refresh_ms // 1000} с")


# --- заглушки состояния -----------------------------------------------------
def _render_no_session() -> None:
    st.markdown("### Наблюдение в реальном времени")
    st.info("Сессия не выбрана. Выберите её в боковой панели или на странице «Home».")


def _render_completed_banner() -> None:
    st.warning("Сессия завершена — показано последнее окно перед окончанием. "
               "Автообновление отключено.")


# --- шапка ------------------------------------------------------------------
def render_header(header: SessionHeader, *, db_ok: bool, paused: bool) -> None:
    is_active = header.ended_at is None
    end = header.ended_at or datetime.now(timezone.utc)
    dur = format_hms(int((end - header.started_at).total_seconds()))

    db_dot, db_txt = ("🟢", "БД доступна") if db_ok else ("🔴", "БД недоступна")
    if not is_active:
        ar_icon, ar_txt = "⏹", "обновление остановлено"
    elif paused:
        ar_icon, ar_txt = "⏸", "обновление на паузе"
    else:
        ar_icon, ar_txt = "▶", "обновление активно"
    sess_dot, sess_txt = ("🟢", "Сессия активна") if is_active else ("🛑", "Сессия завершена")

    left, right = st.columns([3, 4])
    with left:
        st.markdown(f"### Наблюдение в реальном времени — сессия #{header.session_id}")
    with right:
        st.markdown(
            f"<div style='text-align:right;padding-top:0.7rem;color:{_SUB_COLOR};"
            "font-size:0.9rem;font-variant-numeric:tabular-nums'>"
            f"{db_dot} {db_txt}　·　{ar_icon} {ar_txt}　·　"
            f"{sess_dot} {sess_txt} · начата {format_utc(header.started_at)} · {dur}"
            "</div>",
            unsafe_allow_html=True,
        )


# --- KPI (переиспользуем .kpi-grid/.kpi-card/.kpi-label/.kpi-value из роутера) ----
def _kpi_card(label: str, value: str, value_color: str | None = None) -> str:
    style = f" style='color:{value_color}'" if value_color else ""
    return (
        "<div class='kpi-card'>"
        f"<div class='kpi-label'>{label}</div>"
        f"<div class='kpi-value'{style}>{value}</div>"
        "</div>"
    )


def render_kpi(kpi: LiveKpi) -> None:
    share = "—" if kpi.rtk_fixed_share is None else f"{kpi.rtk_fixed_share * 100:.1f} %"
    age = "—" if kpi.last_age_s is None else f"{kpi.last_age_s:.1f} с"
    sig = "—" if kpi.last_sigma_h_m is None else f"{kpi.last_sigma_h_m:.3f} м"
    cards = (
        _kpi_card("Эпох в окне", str(kpi.epochs_in_window))
        + _kpi_card("Доля RTK fixed (rover_rtk)", share, kpi_share_color(kpi.rtk_fixed_share))
        + _kpi_card("Возраст коррекций (rover_rtk)", age, kpi_age_color(kpi.last_age_s))
        + _kpi_card("σ горизонтальная (rover_rtk)", sig, kpi_sigma_color(kpi.last_sigma_h_m))
    )
    st.markdown(f"<div class='kpi-grid'>{cards}</div>", unsafe_allow_html=True)


# --- карточки каналов (шасси .kpi-card, точечные правки раскладки) -----------
def _channel_card(stream: str, row: pd.Series | None, secs_since: float | None) -> str:
    emoji, label, color = format_channel_status(secs_since)
    badge = (
        f"<span style='background:{color}1a;color:{color};border:1px solid {color}55;"
        "border-radius:999px;padding:2px 10px;font-size:.78rem;white-space:nowrap'>"
        f"{emoji} {label}</span>"
    )
    if row is None:
        body = "<div class='kpi-sub'>нет эпох</div>"
    else:
        mode = int(row["solution_mode"]) if pd.notna(row["solution_mode"]) else None
        sats = "—" if pd.isna(row["satellites_used"]) else int(row["satellites_used"])
        hdop = "—" if pd.isna(row["hdop"]) else f"{float(row['hdop']):.1f}"
        mode_col = solution_mode_color(mode)
        body = (
            f"<div class='kpi-sub'>Последняя эпоха: {format_utc(row['epoch_time'])}</div>"
            f"<div class='kpi-sub'>Режим: "
            f"<b style='color:{mode_col}'>{format_solution_mode(mode)}</b></div>"
            f"<div class='kpi-sub'>Спутники: {sats} · HDOP {hdop}</div>"
        )
    return (
        "<div class='kpi-card' style='justify-content:flex-start;gap:10px;min-height:140px'>"
        "<div style='display:flex;justify-content:space-between;align-items:center'>"
        f"<span class='kpi-label' style='letter-spacing:.02em'>{stream}</span>{badge}</div>"
        f"<div style='display:flex;flex-direction:column;gap:4px'>{body}</div></div>"
    )


def render_channel_status(last_df: pd.DataFrame) -> None:
    cols = st.columns(3)
    for col, stream in zip(cols, _STATUS_STREAMS, strict=True):
        with col:
            sel = last_df[last_df["stream_id"] == stream]
            if sel.empty:
                st.markdown(_channel_card(stream, None, None), unsafe_allow_html=True)
                continue
            r = sel.iloc[0]
            secs = float((r["server_now"] - r["epoch_time"]).total_seconds())
            st.markdown(_channel_card(stream, r, secs), unsafe_allow_html=True)


# --- блок RTCM --------------------------------------------------------------
def _rtcm_rate_figure(rate_df: pd.DataFrame) -> go.Figure:
    fig = go.Figure()
    for i, mt in enumerate(sorted(rate_df["msg_type"].unique())):
        sub = rate_df[rate_df["msg_type"] == mt]
        fig.add_bar(x=sub["bucket_time"], y=sub["msg_per_sec"],
                    name=f"{int(mt)} ({rtcm_type_label(int(mt))})",
                    marker_color=_RTCM_PALETTE[i % len(_RTCM_PALETTE)])
    apply_ops_theme(fig)
    fig.update_layout(barmode="stack", height=200, showlegend=True,
                      legend=dict(orientation="v", x=1.01, y=1, font=dict(size=10)))
    fig.update_yaxes(title_text="сообщений/с")
    fig.update_xaxes(title_text="Время UTC")
    return fig


def render_rtcm_block(status: RtcmStatus | None, rate_df: pd.DataFrame) -> None:
    with st.container(border=True):
        head_l, head_r = st.columns([2, 3])
        with head_l:
            st.markdown("**Поток RTCM**")
        if status is None or status.last_received_at is None:
            with head_r:
                st.markdown("<div style='text-align:right;color:#ef4444'>🛑 нет данных</div>",
                            unsafe_allow_html=True)
            st.caption("По этой сессии нет записей RTCM.")
            return
        secs = float((status.server_now - status.last_received_at).total_seconds())
        emoji, label, color = format_channel_status(secs)
        with head_r:
            st.markdown(
                f"<div style='text-align:right;color:{_SUB_COLOR}'>"
                f"<span style='color:{color}'>{emoji} {label}</span>"
                f" · Последнее сообщение: {format_utc(status.last_received_at)}</div>",
                unsafe_allow_html=True,
            )
        st.caption(f"Скорость: {status.msg_per_sec:.1f} сообщений/с · "
                   f"Полоса: {status.bytes_per_sec / 1024:.1f} КБ/с · "
                   f"Типов в окне: {status.distinct_types}")
        if rate_df.empty:
            st.caption("Нет данных для графика за окно.")
            return
        st.plotly_chart(_rtcm_rate_figure(rate_df), use_container_width=True,
                        config={"displayModeBar": False})


# --- strip режима -----------------------------------------------------------
def _render_mode_legend() -> None:
    items = [(4, "RTK fixed"), (5, "RTK float"), (2, "DGPS"), (1, "SPP"), (0, "Нет решения")]
    spans = "　".join(f"<span style='color:{solution_mode_color(c)}'>●</span> {t}"
                     for c, t in items)
    st.markdown(f"<div style='text-align:center;color:{_SUB_COLOR};font-size:0.85rem'>"
                f"{spans}</div>", unsafe_allow_html=True)


def render_mode_strip(epoch_df: pd.DataFrame, allowed_modes: frozenset[int]) -> None:
    st.markdown("**Режим решения по времени**")
    if epoch_df.empty:
        st.caption("Нет эпох за окно.")
        return
    fig = go.Figure()
    for stream, y in (("rover_rtk", 1), ("rover_spp", 0)):
        sub = epoch_df[epoch_df["stream_id"] == stream]
        if sub.empty:
            continue
        colors = [solution_mode_color(int(m)) if int(m) in allowed_modes else _STRIP_MUTED
                  for m in sub["solution_mode"]]
        fig.add_scatter(x=sub["epoch_time"], y=[y] * len(sub), mode="markers",
                        marker=dict(color=colors, size=12, symbol="square"),
                        hovertext=[format_solution_mode(int(m)) for m in sub["solution_mode"]],
                        hoverinfo="x+text", showlegend=False)
    apply_ops_theme(fig)
    fig.update_layout(height=140)
    fig.update_yaxes(tickmode="array", tickvals=[0, 1],
                     ticktext=["rover_spp", "rover_rtk"], range=[-0.6, 1.6])
    fig.update_xaxes(title_text="Время UTC")
    st.plotly_chart(fig, use_container_width=True, config={"displayModeBar": False})
    _render_mode_legend()


# --- σ_h (две оси, мягкий клиппинг) -----------------------------------------
def render_sigma_h(epoch_df: pd.DataFrame) -> None:
    st.markdown("**σ горизонтальная, м**")
    if epoch_df.empty:
        st.caption("Нет эпох за окно.")
        return
    df = epoch_df.copy()
    df["sigma_h"] = (df["sigma_east_m"] ** 2 + df["sigma_north_m"] ** 2) ** 0.5
    rtk = df[df["stream_id"] == "rover_rtk"].dropna(subset=["sigma_h"])
    spp = df[df["stream_id"] == "rover_spp"].dropna(subset=["sigma_h"])
    fig = go.Figure()
    if not rtk.empty:
        fig.add_scatter(x=rtk["epoch_time"], y=rtk["sigma_h"], name="rover_rtk (левая ось)",
                        line=dict(color="#22c55e"), yaxis="y")
    if not spp.empty:
        fig.add_scatter(x=spp["epoch_time"], y=spp["sigma_h"], name="rover_spp (правая ось)",
                        line=dict(color="#3b82f6"), yaxis="y2")
    rtk_top = _soft_top(rtk["sigma_h"].max() if not rtk.empty else None, 0.1)
    spp_top = _soft_top(spp["sigma_h"].max() if not spp.empty else None, 5.0)
    apply_ops_theme(fig)
    fig.update_layout(
        margin=dict(l=52, r=52, t=28, b=34),
        yaxis=dict(title="rover_rtk, м", range=[0, rtk_top],
                   title_font_color="#22c55e", tickfont_color="#22c55e"),
        yaxis2=dict(title="rover_spp, м", range=[0, spp_top], overlaying="y", side="right",
                    title_font_color="#3b82f6", tickfont_color="#3b82f6"),
        showlegend=True, legend=dict(orientation="h", x=0, y=1.18, font=dict(size=10)),
    )
    fig.update_xaxes(title_text="Время UTC")
    st.plotly_chart(fig, use_container_width=True, config={"displayModeBar": False})


# --- возраст коррекций ------------------------------------------------------
def render_age(epoch_df: pd.DataFrame) -> None:
    st.markdown("**Возраст коррекций rover_rtk, с**")
    rtk = (epoch_df[epoch_df["stream_id"] == "rover_rtk"]
           .dropna(subset=["age_of_corrections_s"]))
    if rtk.empty:
        st.caption("Нет эпох rover_rtk за окно.")
        return
    fig = go.Figure()
    fig.add_scatter(x=rtk["epoch_time"], y=rtk["age_of_corrections_s"],
                    line=dict(color="#22c55e"), showlegend=False)
    apply_ops_theme(fig)
    fig.add_hline(y=AGE_WARN_S, line=dict(color="#f59e0b", dash="dash"),
                  annotation_text="внимание", annotation_position="top right")
    top = _soft_top(float(rtk["age_of_corrections_s"].max()), 10.0)
    fig.update_yaxes(title_text="с", range=[0, top])
    fig.update_xaxes(title_text="Время UTC")
    st.plotly_chart(fig, use_container_width=True, config={"displayModeBar": False})


# --- точка входа страницы (set_page_config теперь в роутере Home.py) ---------
def main() -> None:
    cfg = get_app_config()
    window_s = cfg.gui.live_window_seconds
    auto_refresh_ms = cfg.gui.auto_refresh_ms

    session_id = render_session_selector(load_sessions())  # расхождение 3: передаём sessions_df
    if session_id is None:
        _render_no_session()
        return

    paused = _render_pause_control()
    allowed_modes = _render_mode_filter()

    header = load_session_header(session_id)
    if header is None:
        st.error(f"Сессия #{session_id} не найдена.")
        return
    is_active = header.ended_at is None

    if is_active and not paused:
        st_autorefresh(interval=auto_refresh_ms, key="live_autorefresh")

    db_ok = check_db_health()
    render_header(header, db_ok=db_ok, paused=paused)
    if not is_active:
        _render_completed_banner()

    epoch_df = load_window_epochs(session_id, window_s)
    _vspace()
    render_kpi(compute_live_kpi(epoch_df))

    _vspace()
    render_channel_status(load_last_epoch_per_stream(session_id))

    _vspace()
    render_rtcm_block(load_rtcm_status(session_id, window_s),
                      load_rtcm_rate_timeseries(session_id, window_s))

    _vspace()
    render_mode_strip(epoch_df, allowed_modes)
    filtered = epoch_df[epoch_df["solution_mode"].isin(allowed_modes)]
    render_sigma_h(filtered)
    render_age(filtered)

    _render_sidebar_footer(window_s, auto_refresh_ms)


main()
