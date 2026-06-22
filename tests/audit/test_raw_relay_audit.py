from __future__ import annotations

import asyncio
import random
from datetime import UTC, datetime

from ntrip_accuracy_monitor.application.audit.rtcm_audit_writer import (
    RtcmAuditWriter,
)
from ntrip_accuracy_monitor.persistence.rtcm_repository import RtcmMessageRecord
from ntrip_accuracy_monitor.protocols.ntrip._framer import crc24q, extract_msg_type
from ntrip_accuracy_monitor.protocols.rtcm.adapter import RtcmMessage


def _make_frame(msg_type: int, body: bytes) -> bytes:
    payload = bytes([(msg_type >> 4) & 0xFF, (msg_type & 0x0F) << 4]) + body
    length = len(payload)
    head = bytes([0xD3, (length >> 8) & 0x03, length & 0xFF])
    frame = head + payload
    crc = crc24q(frame)
    return frame + bytes([(crc >> 16) & 0xFF, (crc >> 8) & 0xFF, crc & 0xFF])


def _v2_filler(n: int, rng: random.Random) -> bytes:
    # Имитация RTCM 2.x 6-of-8: байты в диапазоне 0x40..0x7F.
    return bytes(0x40 + rng.getrandbits(6) for _ in range(n))


class _StubAdapter:
    """Заглушка RtcmAdapter: тип из сырых байт, без pyrtcm."""

    def parse(self, raw: bytes) -> RtcmMessage:
        return RtcmMessage(
            raw=raw,
            message_type=extract_msg_type(raw[3:]),
            received_at=datetime.now(UTC),
        )


class _FakeRepository:
    def __init__(self) -> None:
        self.batches: list[list[RtcmMessageRecord]] = []

    async def insert_batch(
        self, session_id: int, batch: list[RtcmMessageRecord],
    ) -> None:
        self.batches.append(list(batch))


async def test_consume_hub_extracts_rtcm3_skips_rtcm2() -> None:
    rng = random.Random(7)
    filler = _v2_filler(50, rng)
    stream = (
        filler
        + _make_frame(1006, bytes(21))
        + _v2_filler(30, rng)
        + _make_frame(1007, b"\x00ADVNULLANTENNA\x00")
        + _v2_filler(40, rng)
        + _make_frame(1033, b"\x00\x00UNICORE UM980\x00")
        + _v2_filler(25, rng)
    )

    queue: asyncio.Queue[bytes | None] = asyncio.Queue()
    pos = 0
    while pos < len(stream):
        step = rng.choice([1, 7, 64, 200, 4096])
        queue.put_nowait(stream[pos:pos + step])
        pos += step
    queue.put_nowait(None)  # sentinel: конец потока

    repo = _FakeRepository()
    writer = RtcmAuditWriter(
        adapter=_StubAdapter(),  # type: ignore[arg-type]
        repository=repo,  # type: ignore[arg-type]
        session_id_provider=lambda: 1,
        max_buffer_size=500,
    )

    await writer.consume_hub(queue)

    assert writer.frames_received == 3
    assert writer.frames_parsed == 3
    assert writer.parse_failures == 0
    assert writer.resync_bytes == len(stream) - sum(
        len(_make_frame(mt, b)) for mt, b in (
            (1006, bytes(21)),
            (1007, b"\x00ADVNULLANTENNA\x00"),
            (1033, b"\x00\x00UNICORE UM980\x00"),
        )
    )
    assert [r.msg_type for r in repo.batches[-1]] == [1006, 1007, 1033]
    assert writer.written_total == 3
