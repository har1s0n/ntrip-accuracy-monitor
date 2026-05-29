from __future__ import annotations

import pandas as pd
import streamlit as st

from ntrip_accuracy_monitor.gui._data import load_sessions
from ntrip_accuracy_monitor.gui._format import format_duration, format_utc
from ntrip_accuracy_monitor.gui._sidebar import render_session_selector

_TERMINATION_LABEL: dict[str, str] = {
    "normal": "✅ штатно",
    "signal": "⏹️ останов оператором",
    "error": "❌ сбой",
}


def _render_header() -> None:
    st.title("NTRIP Accuracy Monitor")
    st.caption(
        "Дашборд только для просмотра. Управление потоками и конфигурацией "
        "выполняется на стороне backend."
    )


def _render_kpis(sessions_df: pd.DataFrame) -> None:
    total = len(sessions_df)
    active = int(sessions_df["is_active"].sum())
    epochs_total = int(sessions_df["epochs_count_total"].sum())
    last_start = sessions_df["started_at"].max()

    epochs_str = f"{epochs_total:,}".replace(",", " ")
    if pd.notna(last_start):
        last_date = last_start.strftime("%Y-%m-%d")
        last_time = last_start.strftime("%H:%M UTC")
    else:
        last_date, last_time = "—", "&nbsp;"

    html = f"""
    <div class="kpi-grid">
      <div class="kpi-card">
        <div class="kpi-label">Сеансов всего</div>
        <div>
          <div class="kpi-value">{total}</div>
          <div class="kpi-sub">&nbsp;</div>
        </div>
      </div>
      <div class="kpi-card">
        <div class="kpi-label">Активных</div>
        <div>
          <div class="kpi-value">{active}</div>
          <div class="kpi-sub">&nbsp;</div>
        </div>
      </div>
      <div class="kpi-card">
        <div class="kpi-label">Эпох всего</div>
        <div>
          <div class="kpi-value">{epochs_str}</div>
          <div class="kpi-sub">&nbsp;</div>
        </div>
      </div>
      <div class="kpi-card">
        <div class="kpi-label">Последний старт</div>
        <div>
          <div class="kpi-value">{last_date}</div>
          <div class="kpi-sub">{last_time}</div>
        </div>
      </div>
    </div>
    """
    st.markdown(html, unsafe_allow_html=True)


def _status(row: pd.Series) -> str:
    if row["is_active"]:
        return "🟢 идёт"
    return _TERMINATION_LABEL.get(row["termination_reason"] or "", "—")


def _to_display(sessions_df: pd.DataFrame) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "Сеанс": sessions_df["session_id"],
            "Начало (UTC)": sessions_df["started_at"].map(format_utc),
            "Окончание (UTC)": sessions_df["ended_at"].map(
                lambda v: format_utc(v) if pd.notna(v) else "— (идёт)"
            ),
            "Длительность": sessions_df["duration_s"].map(format_duration),
            "Статус": sessions_df.apply(_status, axis=1),
            "Эпох": sessions_df["epochs_count_total"],
            "Каналы": sessions_df["streams"].map(
                lambda lst: ", ".join(lst) if lst else "—"
            ),
            "Описание": sessions_df["description"],
        }
    )


def _render_sessions_table(sessions_df: pd.DataFrame) -> None:
    st.subheader("Сеансы наблюдений")
    st.dataframe(
        _to_display(sessions_df),
        hide_index=True,
        use_container_width=True,
        column_config={
            "Сеанс": st.column_config.NumberColumn(width="small", format="%d"),
            "Эпох": st.column_config.NumberColumn(
                help="Всего эпох в сеансе по всем каналам",
                format="%d",
            ),
            "Описание": st.column_config.TextColumn(width="large"),
        },
    )


def _render_session_links(selected_id: int) -> None:
    st.subheader(f"Открыть сеанс №{selected_id}:")
    c1, c2, c3 = st.columns(3)
    with c1:
        st.page_link("pages/1_Live_monitor.py",
                     label="📡 Наблюдение в реальном времени")
    with c2:
        st.page_link("pages/2_Session_report.py", label="📊 Отчёт по сеансу")
    with c3:
        st.page_link("pages/3_Compare.py", label="⚖️ Сравнение A / B")
    st.caption(
        "Выбор сеанса сохраняется между страницами через `?session=` в URL "
        "и `st.session_state`."
    )


def main() -> None:
    _render_header()

    sessions_df: pd.DataFrame = load_sessions()
    selected_id: int | None = render_session_selector(sessions_df)

    if sessions_df.empty:
        st.info("Сеансов пока нет. Запустите backend-сервис, чтобы записать данные.")
        return

    _render_kpis(sessions_df)
    st.divider()
    _render_sessions_table(sessions_df)

    if selected_id is not None:
        st.divider()
        _render_session_links(selected_id)


main()
