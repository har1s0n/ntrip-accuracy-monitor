"""Тесты FileRtcmSource."""

from __future__ import annotations

from pathlib import Path

import pytest

from ntrip_accuracy_monitor.tools.replay import FileRtcmSource


@pytest.mark.asyncio
async def test_clean_file_yields_all_frames(synth_rtcm_clean: Path) -> None:
    """Чистый RTCM-файл - все кадры доходят, ничего не отбрасывается."""
    src = FileRtcmSource(synth_rtcm_clean)
    frames = [frame async for frame in src]
    assert len(frames) == 3
    assert all(f.startswith(b"\xd3") for f in frames)
    assert src.frames_received == 3
    assert src.bytes_dropped == 0


@pytest.mark.asyncio
async def test_mixed_file_yields_only_valid_frames(synth_rtcm_mixed: Path) -> None:
    """Мусор между кадрами отбрасывается, счётчик растёт."""
    src = FileRtcmSource(synth_rtcm_mixed)
    frames = [frame async for frame in src]
    assert len(frames) == 3
    assert src.frames_received == 3
    # три участка мусора по 48 байт = 144 (точное значение зависит от framer'а;
    # проверяем, что счётчик не нулевой и в разумных пределах)
    assert 0 < src.bytes_dropped <= 200


@pytest.mark.asyncio
async def test_empty_file_yields_nothing(synth_rtcm_empty: Path) -> None:
    """Пустой файл — корректное завершение итерации без исключений."""
    src = FileRtcmSource(synth_rtcm_empty)
    frames = [frame async for frame in src]
    assert frames == []
    assert src.frames_received == 0


@pytest.mark.asyncio
async def test_truncated_last_frame_is_dropped(synth_rtcm_truncated: Path) -> None:
    """Если последний кадр обрезан — он не отдается, но первый цел."""
    src = FileRtcmSource(synth_rtcm_truncated)
    frames = [frame async for frame in src]
    assert len(frames) == 1
    assert src.frames_received == 1


@pytest.mark.asyncio
async def test_nonexistent_file_does_not_crash(tmp_path: Path) -> None:
    """Несуществующий путь — итерация пустая, не падаем."""
    src = FileRtcmSource(tmp_path / "no_such_file.bin")
    frames = [frame async for frame in src]
    assert frames == []
    assert src.frames_received == 0


@pytest.mark.asyncio
async def test_aclose_is_idempotent(synth_rtcm_clean: Path) -> None:
    """Метод aclose не падает при многократном вызове."""
    src = FileRtcmSource(synth_rtcm_clean)
    await src.aclose()
    await src.aclose()


@pytest.mark.asyncio
async def test_aclose_before_iteration_yields_nothing(synth_rtcm_clean: Path) -> None:
    """Если закрыть до начала чтения — итерация ничего не отдаст."""
    src = FileRtcmSource(synth_rtcm_clean)
    await src.aclose()
    frames = [frame async for frame in src]
    assert frames == []
