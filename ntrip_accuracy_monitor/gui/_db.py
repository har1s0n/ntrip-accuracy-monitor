from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any

import asyncpg
import streamlit as st
import json

from ntrip_accuracy_monitor.application.config import AppConfig, load_config


def _config_path() -> Path:
    """Путь до config.toml. Переопределяется переменной окружения NAM_CONFIG."""
    return Path(os.environ.get("NAM_CONFIG", "config.toml"))


@st.cache_resource
def get_app_config() -> AppConfig:
    """
    Загружается один раз на жизнь Streamlit-процесса (через @st.cache_resource).

    Пароли подмешиваются из env (PG_PASSWORD обязательна, см. load_config).
    Если PG_PASSWORD не задана при первом обращении — Streamlit покажет
    ValueError на странице, что соответствует разработческому режиму запуска.
    """
    return load_config(_config_path())


async def _fetch(sql: str, params: tuple[Any, ...]) -> list[asyncpg.Record]:
    cfg = get_app_config().postgres
    conn = await asyncpg.connect(
        host=cfg.host,
        port=cfg.port,
        user=cfg.user,
        password=cfg.password.get_secret_value(),
        database=cfg.database,
    )
    try:
        return await conn.fetch(sql, *params)
    finally:
        await conn.close()


def fetch_records(sql: str, *params: Any) -> list[asyncpg.Record]:
    """
    Синхронный фасад для Streamlit. Открывает короткоживущий asyncpg-коннект
    через asyncio.run, выполняет SELECT, закрывает соединение.
    """
    return asyncio.run(_fetch(sql, params))


async def _fetch(sql: str, params: tuple[Any, ...]) -> list[asyncpg.Record]:
    cfg = get_app_config().postgres
    conn = await asyncpg.connect(
        host=cfg.host,
        port=cfg.port,
        user=cfg.user,
        password=cfg.password.get_secret_value(),
        database=cfg.database,
    )
    try:
        await conn.set_type_codec(
            "jsonb",
            encoder=json.dumps,
            decoder=json.loads,
            schema="pg_catalog",
        )
        return await conn.fetch(sql, *params)
    finally:
        await conn.close()
