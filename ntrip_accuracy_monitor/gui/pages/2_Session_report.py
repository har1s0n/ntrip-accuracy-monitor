from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from ntrip_accuracy_monitor.gui._data import (
    SessionDetail,
    load_metrics_by_age,
    load_session_detail,
    load_session_metrics,
    load_sessions,
)
from ntrip_accuracy_monitor.gui._format import (
    ACCENT_TEAL,
    COLOR_NEUTRAL,
    apply_ops_theme,
    format_hms,
    format_termination_reason,
    format_utc,
    termination_reason_color,
    format_meters,
)
from ntrip_accuracy_monitor.gui._sidebar import render_session_selector

# Метрика → колонка в metrics_by_age (radio-переключатель оси Y)
_AGE_METRIC_COL: dict[str, str] = {"HRMS": "hrms_m", "CEP50": "cep50_m", "R95": "r95_m"}

# Стили компонентов, специфичных для этой страницы (панели шапки + бейдж +
# единица в KPI). KPI-сетка/карточки — из Home _CSS; здесь не дублируются.
_PAGE_CSS = """
<style>
  .sr-panel {
      background: linear-gradient(160deg, #18212C 0%, #121922 100%);
      border: 1px solid #223040; border-radius: 14px;
      padding: 18px 22px; box-shadow: 0 1px 2px rgba(0,0,0,.30); height: 100%;
  }
  .sr-header {
      display: grid; grid-template-columns: 1fr 1fr;
      gap: 16px; align-items: stretch;
  }
  .sr-panel-title {
      text-transform: uppercase; letter-spacing: .07em;
      font-size: .72rem; font-weight: 600; color: #3DD6C4; margin-bottom: 12px;
  }
  .sr-row {
      display: grid; grid-template-columns: 160px 1fr; gap: 6px 14px;
      padding: 4px 0; font-size: .95rem;
  }
  .sr-label { color: #7d8b99; }
  .sr-val {
      color: #E6EDF3; font-variant-numeric: tabular-nums; white-space: nowrap;
  }
  .sr-badge {
      display: inline-block; padding: 2px 12px; border-radius: 999px;
      font-size: .82rem; font-weight: 600;
  }
  .kpi-unit { font-size: .95rem; color: #8fa3b3; font-weight: 600; }
</style>
"""


def _inject_css() -> None:
    st.markdown(_PAGE_CSS, unsafe_allow_html=True)


def _deg(value: float | None) -> str:
    return f"{value:.6f}°" if isinstance(value, (int, float)) else "—"


def _val_m(value: float | None) -> str:
    """'0.021 m' с приглушённой единицей; '—' для None/NaN."""
    if value is None or value != value:  # NaN
        return "—"
    return f'{value:.3f}<span class="kpi-unit"> m</span>'


def _val_pct(ratio: float | None) -> str:
    if ratio is None or ratio != ratio:
        return "—"
    return f'{ratio * 100:.1f}<span class="kpi-unit"> %</span>'


def _kpi_card(label: str, value_html: str) -> str:
    return (
        '<div class="kpi-card">'
        f'<div class="kpi-label">{label}</div>'
        f'<div><div class="kpi-value">{value_html}</div></div>'
        "</div>"
    )


def _info_panel(detail: SessionDetail) -> str:
    end = detail.ended_at or datetime.now(timezone.utc)
    duration = format_hms(int((end - detail.started_at).total_seconds()))
    period_end = format_utc(detail.ended_at) if detail.ended_at else "— (идёт)"

    if detail.ended_at is None:
        badge_text, badge_color = "идёт", COLOR_NEUTRAL
    else:
        badge_text = format_termination_reason(detail.termination_reason)
        badge_color = termination_reason_color(detail.termination_reason)
    badge = (
        f'<span class="sr-badge" style="color:{badge_color};'
        f"border:1px solid {badge_color};background:{badge_color}1A;\">"
        f"{badge_text}</span>"
    )

    rows = [
        ("№ сеанса", str(detail.session_id)),
        ("Период (UTC)", f"{format_utc(detail.started_at)} → {period_end}"),
        ("Длительность", duration),
        ("Описание", detail.description or "—"),
        ("Причина завершения", badge),
    ]
    body = "".join(
        f'<div class="sr-row"><div class="sr-label">{label}</div>'
        f'<div class="sr-val">{value}</div></div>'
        for label, value in rows
    )
    return (
        '<div class="sr-panel"><div class="sr-panel-title">ⓘ Информация о сеансе'
        f"</div>{body}</div>"
    )


def _antenna_panel(detail: SessionDetail) -> str:
    ant = detail.reference_antenna
    if not ant:
        body = (
            '<div class="sr-row"><div class="sr-val" '
            'style="color:#7d8b99;">Эталон не задан</div></div>'
        )
    else:
        rows = [
            ("Широта", _deg(ant.get("latitude_deg"))),
            ("Долгота", _deg(ant.get("longitude_deg"))),
            ("Высота", format_meters(ant.get("ellipsoidal_height_m"))),
            ("Источник", str(ant.get("source") or "—")),
        ]
        body = "".join(
            f'<div class="sr-row"><div class="sr-label">{label}</div>'
            f'<div class="sr-val">{value}</div></div>'
            for label, value in rows
        )
    return (
        '<div class="sr-panel"><div class="sr-panel-title">⌖ Эталонная антенна'
        f"</div>{body}</div>"
    )


def render_session_header(detail: SessionDetail) -> None:
    html = (
        '<div class="sr-header">'
        f"{_info_panel(detail)}{_antenna_panel(detail)}"
        "</div>"
    )
    st.markdown(html, unsafe_allow_html=True)


def select_metric_row(metrics_df: pd.DataFrame) -> int:
    """Рендерит сводную таблицу метрик; возвращает индекс выбранной строки."""
    display = pd.DataFrame(
        {
            "Канал": metrics_df["stream_id"],
            "Режим": metrics_df["solution_mode_filter"],
            "Эпох": metrics_df["epochs_after_filter"].astype("int64"),
            "HRMS (м)": metrics_df["hrms_m"],
            "VRMS (м)": metrics_df["vrms_m"],
            "CEP50 (м)": metrics_df["cep50_m"],
            "R95 (м)": metrics_df["r95_m"],
            "Fixed %": metrics_df["fixed_ratio"] * 100.0,
        }
    )
    event = st.dataframe(
        display,
        hide_index=True,
        use_container_width=True,
        on_select="rerun",
        selection_mode="single-row",
        key="sr_metrics_table",
        column_config={
            "Эпох": st.column_config.NumberColumn(format="%d"),
            "HRMS (м)": st.column_config.NumberColumn(format="%.3f"),
            "VRMS (м)": st.column_config.NumberColumn(format="%.3f"),
            "CEP50 (м)": st.column_config.NumberColumn(format="%.3f"),
            "R95 (м)": st.column_config.NumberColumn(format="%.3f"),
            "Fixed %": st.column_config.NumberColumn(format="%.1f"),
        },
    )
    selected = event.selection.rows
    return selected[0] if selected else 0


def render_kpi_cards(row: pd.Series) -> None:
    cards = [
        ("HRMS", _val_m(row["hrms_m"])),
        ("VRMS", _val_m(row["vrms_m"])),
        ("2DRMS", _val_m(row["two_drms_m"])),
        ("CEP50", _val_m(row["cep50_m"])),
        ("R95", _val_m(row["r95_m"])),
        ("3D ошибка макс.", _val_m(row["error_3d_max_m"])),
        ("Fixed ratio", _val_pct(row["fixed_ratio"])),
    ]
    inner = "".join(_kpi_card(label, value) for label, value in cards)
    html = (
        '<div class="kpi-grid" '
        f'style="grid-template-columns:repeat(7,1fr);gap:12px;">{inner}</div>'
    )
    st.markdown(html, unsafe_allow_html=True)


def render_age_chart(session_id: int, stream_id: str, mode_filter: str) -> None:
    title_col, radio_col = st.columns([3, 2])
    with title_col:
        st.subheader("Ошибка от возраста коррекций")
    with radio_col:
        metric = st.radio(
            "Метрика",
            list(_AGE_METRIC_COL),
            horizontal=True,
            label_visibility="collapsed",
            key="sr_age_metric",
        )

    age_df = load_metrics_by_age(session_id, stream_id, mode_filter)
    if age_df.empty:
        st.info("Нет зависимости от возраста коррекций для выбранного режима.")
        return

    col = _AGE_METRIC_COL[metric]
    sig = age_df[age_df["is_significant"]]
    insig = age_df[~age_df["is_significant"]]

    fig = go.Figure()
    if not sig.empty:
        fig.add_trace(
            go.Scatter(
                x=sig["age_mid_s"], y=sig[col], mode="lines+markers",
                line=dict(color=ACCENT_TEAL, width=2),
                marker=dict(color=ACCENT_TEAL, size=7),
            )
        )
    if not insig.empty:
        fig.add_trace(
            go.Scatter(
                x=insig["age_mid_s"], y=insig[col], mode="markers",
                marker=dict(color=ACCENT_TEAL, size=7, opacity=0.3),
            )
        )
    apply_ops_theme(fig)
    fig.update_layout(height=340, showlegend=False)
    fig.update_xaxes(title_text="возраст коррекций, с")
    fig.update_yaxes(title_text="ошибка, м")
    st.plotly_chart(fig, use_container_width=True)


def render_config(detail: SessionDetail) -> None:
    with st.expander("Снимок конфигурации", expanded=False):
        if detail.config_snapshot:
            st.json(detail.config_snapshot)
        else:
            st.caption("Нет снимка конфигурации.")


def main() -> None:
    st.title("Отчёт по сеансу")
    _inject_css()

    sessions_df = load_sessions()
    if sessions_df.empty:
        st.info("Сеансов пока нет. Запустите backend-сервис, чтобы записать данные.")
        return

    selected_id = render_session_selector(sessions_df)
    if selected_id is None:
        st.info("Выберите сеанс для просмотра отчёта.")
        return

    detail = load_session_detail(selected_id)
    if detail is None:
        st.warning(f"Сеанс №{selected_id} не найден.")
        return

    render_session_header(detail)
    st.divider()

    metrics_df = load_session_metrics(selected_id)
    if metrics_df.empty:
        st.info("Для этого сеанса ещё нет рассчитанных метрик (session_metrics).")
        render_config(detail)
        return

    idx = min(select_metric_row(metrics_df), len(metrics_df) - 1)
    row = metrics_df.iloc[idx]
    render_kpi_cards(row)

    st.divider()
    render_age_chart(
        selected_id, str(row["stream_id"]), str(row["solution_mode_filter"])
    )

    st.divider()
    render_config(detail)


main()
