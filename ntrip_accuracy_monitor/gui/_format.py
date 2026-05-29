from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    import plotly.graph_objects as go

ACCENT_TEAL: Final = "#3DD6C4"

_TERMINATION_LABELS: Final[dict[str, str]] = {
    "normal": "штатно",
    "signal": "остановленно оператором",
    "error": "сбой",
}


def format_termination_reason(reason: str | None) -> str:
    if reason is None:
        return "—"
    return _TERMINATION_LABELS.get(reason, reason)


def termination_reason_color(reason: str | None) -> str:
    match reason:
        case "normal":
            return COLOR_OK
        case "signal":
            return COLOR_WARN
        case "error":
            return COLOR_BAD
        case _:
            return COLOR_NEUTRAL


def format_utc(dt: datetime | None) -> str:
    """'YYYY-MM-DD HH:MM:SS UTC' либо '—' для None/NaT."""
    if dt is None:
        return "—"
    if dt.tzinfo is None:
        raise ValueError("format_utc requires timezone-aware datetime")
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def format_meters(value: float | None, decimals: int = 3) -> str:
    """'1.234 m' либо '—' для None/NaN."""
    if value is None or value != value:  # NaN check без numpy
        return "—"
    return f"{value:.{decimals}f} m"


def format_percent(ratio: float | None, decimals: int = 1) -> str:
    """'42.0 %' для ratio в [0,1]; '—' для None/NaN."""
    if ratio is None or ratio != ratio:
        return "—"
    return f"{ratio * 100:.{decimals}f} %"


def format_duration(seconds: float | None) -> str:
    """'1h 23m 45s'. Опускает нулевые ведущие компоненты."""
    if seconds is None or seconds != seconds:
        return "—"
    total = int(seconds)
    sign = "-" if total < 0 else ""
    total = abs(total)
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{sign}{h}h {m}m {s}s"
    if m:
        return f"{sign}{m}m {s}s"
    return f"{sign}{s}s"


# --- Цветовая семантика режима решения (GGA quality) ------------------------
_GREY: Final = "#94a3b8"

SOLUTION_MODE_COLORS: Final[dict[int, str]] = {
    0: _GREY,  # Нет решения
    1: "#3b82f6",  # SPP — синий
    2: "#f97316",  # DGPS — оранжевый
    4: "#22c55e",  # RTK fixed — зелёный
    5: "#eab308",  # RTK float — янтарный
    6: _GREY,  # Счисление
    7: _GREY,  # Ручной ввод
    8: _GREY,  # Симуляция
}

_SOLUTION_MODE_LABELS: Final[dict[int, str]] = {
    0: "Нет решения", 1: "SPP", 2: "DGPS", 4: "RTK fixed",
    5: "RTK float", 6: "Счисление", 7: "Ручной ввод", 8: "Симуляция",
}

RTCM_TYPE_LABELS: Final[dict[int, str]] = {
    1004: "наблюдения GPS",
    1012: "наблюдения GLONASS",
    1019: "эфемериды GPS",
    1020: "эфемериды GLONASS",
    1033: "дескриптор станции",
    1006: "координаты базы",
}

# --- Пороги (вынос в config — позже) -----------------------
RTK_FIXED_SHARE_OK: Final = 0.95
RTK_FIXED_SHARE_WARN: Final = 0.80
AGE_OK_S: Final = 2.0
AGE_WARN_S: Final = 5.0
SIGMA_H_OK_M: Final = 0.05
SIGMA_H_WARN_M: Final = 0.20
STALE_FACTOR_LAG: Final = 3.0  # 3× период → задержка
STALE_FACTOR_DEAD: Final = 10.0  # 10× период → нет данных

COLOR_OK: Final = "#22c55e"
COLOR_WARN: Final = "#eab308"
COLOR_BAD: Final = "#ef4444"
COLOR_NEUTRAL: Final = "#cbd5e1"


def format_solution_mode(code: int | None) -> str:
    if code is None:
        return "—"
    return _SOLUTION_MODE_LABELS.get(code, f"Режим {code}")


def solution_mode_color(code: int | None) -> str:
    if code is None:
        return _GREY
    return SOLUTION_MODE_COLORS.get(code, _GREY)


def rtcm_type_label(msg_type: int) -> str:
    return RTCM_TYPE_LABELS.get(msg_type, f"тип {msg_type}")


def format_channel_status(
    seconds_since: float | None, expected_period_s: float = 1.0
) -> tuple[str, str, str]:
    """→ (emoji, подпись, цвет) для бэйджа статуса канала."""
    if seconds_since is None or seconds_since > STALE_FACTOR_DEAD * expected_period_s:
        return ("🛑", "нет данных", COLOR_BAD)
    if seconds_since > STALE_FACTOR_LAG * expected_period_s:
        return ("⚪", "задержка", COLOR_WARN)
    return ("🟢", "онлайн", COLOR_OK)


def kpi_share_color(share: float | None) -> str:
    if share is None:
        return COLOR_NEUTRAL
    return COLOR_OK if share >= RTK_FIXED_SHARE_OK else (
        COLOR_WARN if share >= RTK_FIXED_SHARE_WARN else COLOR_BAD
    )


def kpi_age_color(age_s: float | None) -> str:
    if age_s is None:
        return COLOR_NEUTRAL
    return COLOR_OK if age_s <= AGE_OK_S else (
        COLOR_WARN if age_s <= AGE_WARN_S else COLOR_BAD
    )


def kpi_sigma_color(sigma_m: float | None) -> str:
    if sigma_m is None:
        return COLOR_NEUTRAL
    return COLOR_OK if sigma_m <= SIGMA_H_OK_M else (
        COLOR_WARN if sigma_m <= SIGMA_H_WARN_M else COLOR_BAD
    )


def format_hms(total_seconds: int) -> str:
    """Длительность как ЧЧ:ММ:СС (для шапки сессии)."""
    h, rem = divmod(max(total_seconds, 0), 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def apply_ops_theme(fig: go.Figure) -> go.Figure:
    """Единая тема Plotly для всех графиков GUI (под тему Home)."""
    grid = "rgba(255,255,255,0.06)"
    fig.update_layout(
        template="plotly_dark",
        margin=dict(l=52, r=16, t=10, b=34),
        height=220,
        font=dict(family="system-ui, sans-serif", size=12, color="#8fa3b3"),
        plot_bgcolor="rgba(0,0,0,0)",
        paper_bgcolor="rgba(0,0,0,0)",
        hovermode="x unified",
        legend=dict(font=dict(color="#E6EDF3")),
    )
    fig.update_xaxes(showgrid=True, gridcolor=grid, zeroline=False,
                     linecolor=grid, tickcolor=grid)
    fig.update_yaxes(showgrid=True, gridcolor=grid, zeroline=False,
                     linecolor=grid, tickcolor=grid)
    return fig
