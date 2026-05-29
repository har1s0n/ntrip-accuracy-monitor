from __future__ import annotations

import pytest

from ntrip_accuracy_monitor.persistence.migrator import (
    apply_migrations,
    pending_migrations,
)

pytestmark = pytest.mark.asyncio


async def test_pending_empty_after_apply(pool) -> None:
    await apply_migrations(pool)
    assert await pending_migrations(pool) == []


async def test_apply_is_idempotent(pool) -> None:
    await apply_migrations(pool)
    assert await apply_migrations(pool) == []
    assert await pending_migrations(pool) == []
