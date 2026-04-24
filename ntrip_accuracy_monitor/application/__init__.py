"""Прикладной слой — оркестрация и инфраструктура приложения.

* ``config``          — Pydantic-модели конфигурации (TOML через ``tomllib``);
* ``logging_config``  — настройка stdlib ``logging``;
* ``service``         — оркестратор на ``asyncio.TaskGroup``;
* ``aggregator``      — сшивка GGA/GST/GSA по времени
  (``pandas.merge_asof`` с допуском ±0.5 с).
"""
