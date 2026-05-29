# ntrip_accuracy_monitor/gui/_sidebar.py — render_session_selector

from __future__ import annotations

import pandas as pd
import streamlit as st


def _coerce_session_id(value: str | int | None) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def render_session_selector(sessions_df: pd.DataFrame) -> int | None:
    """
    Рендерит в сайдбаре селектор сеанса.

    Источник истины:
        1. st.query_params["session"]      (URL — для page_link и шеринга)
        2. st.session_state["session_id"]  (пережил клики внутри страницы)

    Возвращает выбранный session_id (int) или None, если сеансов нет.
    """
    st.sidebar.header("Сеанс")

    if sessions_df.empty:
        st.sidebar.info("В базе нет сеансов.")
        st.session_state.pop("session_id", None)
        st.query_params.pop("session", None)
        return None

    ids: list[int] = sessions_df["session_id"].tolist()

    initial: int | None = (
        _coerce_session_id(st.query_params.get("session"))
        or _coerce_session_id(st.session_state.get("session_id"))
    )
    if initial not in ids:
        initial = ids[0]

    def _label(sid: int) -> str:
        row = sessions_df.loc[sessions_df["session_id"] == sid].iloc[0]
        marker = "🟢" if bool(row["is_active"]) else "⚪"
        started = row["started_at"].strftime("%Y-%m-%d %H:%M UTC")
        desc = str(row["description"] or "").strip()
        suffix = f" — {desc}" if desc else ""
        return f"{marker} №{sid} · {started}{suffix}"

    selected: int = st.sidebar.selectbox(
        "Выберите сеанс",
        options=ids,
        index=ids.index(initial),
        format_func=_label,
        key="session_id",
    )

    st.query_params["session"] = str(selected)
    return selected
