"""Asterisk AudioSocket wire protocol.

Each message is: 1 byte kind, 2 bytes big-endian payload length, payload.
Audio is 16-bit signed linear mono PCM, normally 20 ms per message. The
AudioSocket() dialplan app only does 8 kHz (kind 0x10); the AudioSocket
channel driver can use wider-band slin formats, each with its own kind.
"""

import asyncio
import struct
import uuid

KIND_HANGUP = 0x00
KIND_UUID = 0x01
KIND_DTMF = 0x03
KIND_AUDIO = 0x10
KIND_ERROR = 0xFF

AUDIO_RATES = {
    0x10: 8000,
    0x11: 12000,
    0x12: 16000,
    0x13: 24000,
    0x14: 32000,
    0x15: 44100,
    0x16: 48000,
    0x17: 96000,
    0x18: 192000,
}
RATE_KINDS = {rate: kind for kind, rate in AUDIO_RATES.items()}

FRAME_MS = 20


def frame_bytes(rate: int) -> int:
    """Bytes in one 20 ms frame of 16-bit mono audio."""
    return rate * FRAME_MS // 1000 * 2


async def read_message(reader: asyncio.StreamReader) -> tuple[int, bytes]:
    header = await reader.readexactly(3)
    kind = header[0]
    (length,) = struct.unpack(">H", header[1:])
    payload = await reader.readexactly(length) if length else b""
    return kind, payload


def encode(kind: int, payload: bytes = b"") -> bytes:
    return struct.pack(">BH", kind, len(payload)) + payload


async def read_call_uuid(reader: asyncio.StreamReader) -> str:
    """The first message on every connection identifies the call."""
    kind, payload = await read_message(reader)
    if kind != KIND_UUID or len(payload) != 16:
        raise ValueError(f"expected UUID message, got kind {kind:#x} ({len(payload)} bytes)")
    return str(uuid.UUID(bytes=payload))


def split_frames(pcm: bytes, rate: int) -> list[bytes]:
    """Split PCM into 20 ms frames, padding the last one with silence."""
    size = frame_bytes(rate)
    frames = [pcm[i : i + size] for i in range(0, len(pcm), size)]
    if frames and len(frames[-1]) < size:
        frames[-1] = frames[-1].ljust(size, b"\x00")
    return frames
