from __future__ import annotations

import streamlit as st

st.set_page_config(
    page_title="NTRIP Accuracy Monitor",
    page_icon="📡",
    layout="wide",
    initial_sidebar_state="expanded",
)

_CSS = """
<style>
  [data-testid="stToolbar"]    { visibility: hidden; height: 0; }
  [data-testid="stDecoration"] { display: none; }
  #MainMenu                    { visibility: hidden; }
  footer                       { visibility: hidden; }

  h1 { letter-spacing: -0.02em; font-weight: 800; }

  [data-testid="stDataFrame"] { font-variant-numeric: tabular-nums; }

  /* ---- KPI-карточки -------------------------------------------------- */
  .kpi-grid {
      display: grid;
      grid-template-columns: repeat(4, 1fr);
      gap: 16px;
      margin: 0.25rem 0 0.5rem;
  }
  .kpi-card {
      display: flex;
      flex-direction: column;
      justify-content: space-between;   /* метка сверху, значение снизу */
      min-height: 132px;
      padding: 20px 22px;
      border-radius: 14px;
      background: linear-gradient(160deg, #18212C 0%, #121922 100%);
      border: 1px solid #223040;
      box-shadow: 0 1px 2px rgba(0, 0, 0, 0.30);
      transition: border-color .18s ease, transform .18s ease, box-shadow .18s ease;
  }
  .kpi-card:hover {
      border-color: #3DD6C4;
      transform: translateY(-2px);
      box-shadow: 0 8px 24px rgba(0, 0, 0, 0.35);
  }
  .kpi-label {
      text-transform: uppercase;
      letter-spacing: 0.07em;
      font-size: 0.72rem;
      font-weight: 600;
      color: #7d8b99;
  }
  .kpi-value {
      font-size: 2rem;
      font-weight: 700;
      line-height: 1;
      color: #F0F4F8;
      white-space: nowrap;              /* дата НЕ переносится */
      font-variant-numeric: tabular-nums;
  }
  .kpi-sub {
      margin-top: 6px;
      font-size: 0.92rem;
      color: #8fa3b3;
      font-variant-numeric: tabular-nums;
  }

  .block-container { padding-top: 2.2rem; }
</style>
"""

st.markdown(_CSS, unsafe_allow_html=True)

# --- Программная навигация с русскими заголовками --------------------------
# Пути в st.Page — относительно директории этого entrypoint-файла (gui/).
# При использовании st.navigation авто-дискавери папки pages/ отключается,
# поэтому числовые префиксы в именах файлов больше не задают порядок —
# порядок определяется этим списком.
_pages: list[st.Page] = [
    st.Page("_overview.py", title="Главная", icon="🏠", default=True),
    st.Page("pages/1_Live_monitor.py",
            title="Наблюдение в реальном времени", icon="📡"),
    st.Page("pages/2_Session_report.py", title="Отчёт по сеансу", icon="📊"),
    st.Page("pages/3_Compare.py", title="Сравнение A / B", icon="⚖️"),
]

st.navigation(_pages).run()
