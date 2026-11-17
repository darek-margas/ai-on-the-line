# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Darek Margas. See NOTICE.
"""Credits on downloaded recordings.

A credit in NOTICE can't be required of people who publish recordings, so it's
made the default instead: a download from the call page carries the credit in
the WAV's metadata and, unless EXPORT_SPOKEN_CREDIT=false, a short spoken line
at the start. The stored recording is never changed.
"""

import io
import struct
import wave

import numpy as np
import soxr

PROJECT = "AI on the Line"
PROJECT_URL = "https://github.com/darek-margas/ai-on-the-line"
SPOKEN_CREDIT = "Recorded with AI on the Line."
# The credit is spoken a little louder than the TTS renders it, so it stands out
# against a quiet phone recording, unless that would clip.
CREDIT_GAIN_DB = 2.0
CREDIT_PEAK = 32000  # just under full scale


def credit_line(voice: str = "") -> str:
    """The text to paste under a published recording."""
    line = f"Made with {PROJECT} ({PROJECT_URL})"
    return f"{line}. Voice: {voice}" if voice else line


def _info_chunk(comment: str) -> bytes:
    """A RIFF LIST/INFO chunk: comment (ICMT) and software (ISFT)."""
    def sub(tag: bytes, text: str) -> bytes:
        data = text.encode("utf-8") + b"\0"
        if len(data) % 2:
            data += b"\0"
        return tag + struct.pack("<I", len(data)) + data

    body = b"INFO" + sub(b"ICMT", comment) + sub(b"ISFT", f"{PROJECT} {PROJECT_URL}")
    return b"LIST" + struct.pack("<I", len(body)) + body


def with_credit(wav: bytes, comment: str, spoken: bytes | None = None, spoken_rate: int = 16000) -> bytes:
    """The recording with the credit in its metadata and, if given, the spoken
    credit (16-bit mono PCM at spoken_rate) in front of it, followed by a short
    pause. Anything that isn't 16-bit PCM keeps its audio as it is."""
    with wave.open(io.BytesIO(wav)) as src:
        params = src.getparams()
        frames = src.readframes(params.nframes)
    if spoken and params.sampwidth == 2:
        speech = np.frombuffer(spoken, dtype=np.int16).astype(np.float32)
        if spoken_rate != params.framerate:
            speech = soxr.resample(speech, spoken_rate, params.framerate)
        peak = float(np.abs(speech).max()) if len(speech) else 0.0
        gain = min(10 ** (CREDIT_GAIN_DB / 20), CREDIT_PEAK / peak) if peak else 1.0
        speech = np.clip(speech * gain, -32768, 32767).astype(np.int16)
        pause = np.zeros(int(params.framerate * 0.6), dtype=np.int16)
        head = np.concatenate([speech, pause])
        if params.nchannels > 1:
            head = np.repeat(head, params.nchannels)
        frames = head.tobytes() + frames
    out = io.BytesIO()
    with wave.open(out, "wb") as dst:
        dst.setnchannels(params.nchannels)
        dst.setsampwidth(params.sampwidth)
        dst.setframerate(params.framerate)
        dst.writeframes(frames)
    riff = bytearray(out.getvalue())
    riff += _info_chunk(comment)
    struct.pack_into("<I", riff, 4, len(riff) - 8)
    return bytes(riff)
