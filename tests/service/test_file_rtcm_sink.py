"""Юнит-тесты FileRtcmSink: открытие/закрытие, запись, обработка ошибок."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from ntrip_accuracy_monitor.application.service.file_rtcm_sink import (
    FileRtcmSink,
)


# ----- конструктор -----
def test_init_rejects_empty_stream_id(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="stream_id"):
        FileRtcmSink(directory=tmp_path, session_id=1, stream_id="   ")


def test_init_rejects_negative_session_id(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="session_id"):
        FileRtcmSink(directory=tmp_path, session_id=-1, stream_id="base")


def test_path_format(tmp_path: Path) -> None:
    sink = FileRtcmSink(
        directory=tmp_path, session_id=42, stream_id="base",
    )
    assert sink.path == tmp_path / "000042_base.bin"


# ----- открытие/закрытие -----
@pytest.mark.asyncio
async def test_aopen_creates_directory(tmp_path: Path) -> None:
    nested = tmp_path / "captures"
    sink = FileRtcmSink(
        directory=nested, session_id=1, stream_id="base",
    )
    await sink.aopen()
    try:
        assert nested.is_dir()
        assert sink.path.is_file()
        assert sink.is_open
    finally:
        await sink.aclose()


@pytest.mark.asyncio
async def test_aclose_idempotent(tmp_path: Path) -> None:
    sink = FileRtcmSink(
        directory=tmp_path, session_id=1, stream_id="base",
    )
    await sink.aopen()
    await sink.aclose()
    await sink.aclose()  # повторный вызов — no-op
    assert not sink.is_open


@pytest.mark.asyncio
async def test_double_aopen_raises(tmp_path: Path) -> None:
    sink = FileRtcmSink(
        directory=tmp_path, session_id=1, stream_id="base",
    )
    await sink.aopen()
    try:
        with pytest.raises(RuntimeError, match="already open"):
            await sink.aopen()
    finally:
        await sink.aclose()


@pytest.mark.asyncio
async def test_async_context_manager(tmp_path: Path) -> None:
    sink = FileRtcmSink(
        directory=tmp_path, session_id=1, stream_id="base",
    )
    async with sink:
        assert sink.is_open
    assert not sink.is_open


# ----- consume_hub -----

@pytest.mark.asyncio
async def test_consume_writes_frames_until_sentinel(tmp_path: Path) -> None:
    sink = FileRtcmSink(
        directory=tmp_path, session_id=7, stream_id="base",
    )
    queue: asyncio.Queue[bytes | None] = asyncio.Queue()
    frames = [b"\xd3\x00\x01\xaa", b"\xd3\x00\x02\xbb\xcc"]
    for f in frames:
        queue.put_nowait(f)
    queue.put_nowait(None)

    async with sink:
        await sink.consume_hub(queue)

    expected = b"".join(frames)
    assert sink.path.read_bytes() == expected
    assert sink.frames_written == 2
    assert sink.bytes_written == len(expected)
    assert sink.write_failures == 0


@pytest.mark.asyncio
async def test_consume_without_open_raises(tmp_path: Path) -> None:
    sink = FileRtcmSink(
        directory=tmp_path, session_id=1, stream_id="base",
    )
    queue: asyncio.Queue[bytes | None] = asyncio.Queue()
    with pytest.raises(RuntimeError, match="aopen"):
        await sink.consume_hub(queue)


@pytest.mark.asyncio
async def test_consume_exits_on_cancel_and_flushes(tmp_path: Path) -> None:
    sink = FileRtcmSink(
        directory=tmp_path, session_id=1, stream_id="base",
    )
    queue: asyncio.Queue[bytes | None] = asyncio.Queue()
    queue.put_nowait(b"\xd3\x00\x01\xaa")

    async with sink:
        task = asyncio.create_task(sink.consume_hub(queue))
        # Дать задаче забрать первый кадр и заблокироваться на следующем get.
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        # finally в consume_hub сделал flush, файл ещё открыт.
        assert sink.frames_written == 1

    # После выхода из async with файл закрыт, содержимое на диске.
    assert sink.path.read_bytes() == b"\xd3\x00\x01\xaa"
