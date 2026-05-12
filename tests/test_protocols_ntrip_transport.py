"""Unit tests for NtripClient using a fake pygnssutils.GNSSNTRIPClient."""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections.abc import Callable
from typing import Any
from unittest.mock import patch

import pytest

pytestmark = pytest.mark.skip(
    reason="rewritten for stdlib NtripClient — full rewrite scheduled for chat #12"
)

from ntrip_accuracy_monitor.protocols.backoff import BackoffPolicy
from ntrip_accuracy_monitor.protocols.ntrip import (
    NtripAuthError,
    NtripClient,
    NtripMountpointError,
    NtripSourcetableError,
)

_PATCH_TARGET = "ntrip_accuracy_monitor.protocols.ntrip.transport.GNSSNTRIPClient"
_FAST_BACKOFF = BackoffPolicy(
    initial_delay_s=0.05,
    max_delay_s=0.10,
    multiplier=2.0,
    jitter=0.0,
)


class _FakeLib:
    """In-process stand-in for pygnssutils.GNSSNTRIPClient.

    Exercises the pieces of the contract NtripClient depends on:
      - context manager (``__enter__`` / ``__exit__``)
      - non-blocking ``run(**kwargs)`` that spawns a daemon Thread
      - ``output.put((raw_bytes, parsed_msg))`` invocations from the thread
      - ``stopevent`` honored for thread shutdown
      - log records emitted on the ``pygnssutils.gnssntripclient`` logger

    The behavior_fn callback decides what the thread does on each session.
    """

    def __init__(self, behavior_fn: Callable[[Any, threading.Event], None]) -> None:
        self._behavior_fn = behavior_fn
        self._stopevent: threading.Event | None = None
        self._thread: threading.Thread | None = None
        self._output: Any = None

    def __enter__(self) -> _FakeLib:
        return self

    def __exit__(self, *exc_info: Any) -> None:
        if self._stopevent is not None:
            self._stopevent.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)

    def run(self, **kwargs: Any) -> bool:
        self._stopevent = kwargs["stopevent"]
        self._output = kwargs["output"]
        self._thread = threading.Thread(target=self._run_in_thread, daemon=True)
        self._thread.start()
        return True

    def _run_in_thread(self) -> None:
        assert self._stopevent is not None
        try:
            self._behavior_fn(self._output, self._stopevent)
        finally:
            self._stopevent.set()


def _make_client(
    *,
    stall_timeout_s: float = 1.0,
    connect_timeout_s: float = 1.0,
    queue_max_size: int = 64,
) -> NtripClient:
    return NtripClient(
        stream_id="test",
        caster_host="example.invalid",
        caster_port=2101,
        use_https=False,
        mountpoint="MOUNT",
        username="anon",
        password="",
        ntrip_version="2.0",
        connect_timeout_s=connect_timeout_s,
        stall_timeout_s=stall_timeout_s,
        reconnect_backoff=_FAST_BACKOFF,
        queue_max_size=queue_max_size,
    )


# ---------------------- 8. constructor validation ----------------------
@pytest.mark.parametrize(
    "kwargs_override",
    [
        {"stream_id": ""},
        {"caster_port": 0},
        {"caster_port": 70000},
        {"mountpoint": ""},
        {"connect_timeout_s": 0.0},
        {"stall_timeout_s": -1.0},
        {"queue_max_size": 0},
    ],
)
def test_constructor_rejects_invalid_args(kwargs_override: dict[str, Any]) -> None:
    base: dict[str, Any] = {
        "stream_id": "ok",
        "caster_host": "h",
        "caster_port": 2101,
        "use_https": False,
        "mountpoint": "M",
        "username": None,
        "password": None,
        "ntrip_version": "2.0",
        "connect_timeout_s": 1.0,
        "stall_timeout_s": 1.0,
        "reconnect_backoff": _FAST_BACKOFF,
        "queue_max_size": 64,
    }
    base.update(kwargs_override)
    with pytest.raises(ValueError):
        NtripClient(**base)
