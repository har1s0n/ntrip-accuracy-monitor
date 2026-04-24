"""PostgreSQL через asyncpg.

* DDL — в ``schema.sql`` (партиционирование по времени, BRIN по epoch_time);
* репозиторий — пул ``asyncpg.Pool`` с БАТЧИРОВАНИЕМ записи
  (``executemany`` / ``COPY``); per-epoch ``INSERT`` в hot path запрещён.

ORM (SQLAlchemy, Tortoise) НЕ используется — только raw SQL с
параметризованными запросами.
"""
