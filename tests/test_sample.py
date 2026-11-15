# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Darek Margas. See NOTICE.
import asyncio
import io
import json
import wave
import zipfile

import numpy as np

from phone_agent.sample import call_sample, recording_sample, redact, speech_segments


def wav_bytes(channels: list[np.ndarray], rate: int = 8000) -> bytes:
    out = io.BytesIO()
    with wave.open(out, "wb") as w:
        w.setnchannels(len(channels))
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(np.stack(channels, axis=1).astype(np.int16).tobytes())
    return out.getvalue()


def tone(seconds: float, rate: int = 8000) -> np.ndarray:
    t = np.arange(int(seconds * rate)) / rate
    # A voice-like buzz: a few harmonics of 150 Hz, so the VAD takes it as speech.
    return sum(np.sin(2 * np.pi * 150 * k * t) / k for k in range(1, 8)) * 6000


def test_redact():
    text = "Call Alex Smith on 0491 570 156 or alex@example.com, booking BBA7Q2K9, that's B B A K Q."
    out = redact(text, ["Alex Smith", "Alex", "Smith"])
    assert out == "Call <owner> on <number> or <email>, booking <ref>, that's <ref>."
    assert redact("It's 2 o'clock, about 20 minutes") == "It's 2 o'clock, about 20 minutes"


def test_call_sample_with_both_sides(tmp_path):
    rec = tmp_path / "phone-agent"
    rec.mkdir()
    for suffix in ("", "-rx", "-tx"):
        (rec / f"abc{suffix}.wav").write_bytes(wav_bytes([tone(1)]))
    call = {
        "id": "abc", "brief": {"direction": "outbound", "goal": "private"}, "recording": "phone-agent/abc.wav",
        "transcript": [["agent", "Hi, I'm Alex's assistant."], ["callee", "Sure, it's 02 5550 1234."]],
        "timings": [0.4, 3.2], "started": 1000.0, "ended": 1030.0, "outcome": "completed", "status": "finished",
        "phone": "0255501234",
    }
    z = zipfile.ZipFile(io.BytesIO(call_sample(call, "Alex", str(tmp_path))))
    assert set(z.namelist()) == {"README.txt", "transcript.json", "transcript.txt", "other.wav", "agent.wav", "call.wav"}
    meta = json.loads(z.read("transcript.json"))
    assert meta["lines"] == [
        {"t": 0.4, "who": "agent", "text": "Hi, I'm <owner>'s assistant."},
        {"t": 3.2, "who": "callee", "text": "Sure, it's <number>."},
    ]
    assert meta["duration"] == 30.0
    dump = json.dumps(meta)
    assert "private" not in dump and "0255501234" not in dump  # no brief, no phone number


def test_call_sample_without_recordings_or_timings():
    call = {"id": "x", "brief": {"direction": "inbound"}, "recording": None, "transcript": [["callee", "Hello?"]]}
    z = zipfile.ZipFile(io.BytesIO(call_sample(call, "", "")))
    assert "other.wav" not in z.namelist()
    assert json.loads(z.read("transcript.json"))["lines"] == [{"t": None, "who": "caller", "text": "Hello?"}]


def test_speech_segments():
    rate = 8000
    pcm = np.concatenate([np.zeros(rate), tone(1.5), np.zeros(rate), tone(1)]).astype(np.int16)
    segments = speech_segments(pcm, rate)
    assert len(segments) == 2
    assert abs(segments[0][0] - 1.0) < 0.2 and abs(segments[1][0] - 3.5) < 0.2


def test_recording_sample(tmp_path):
    rate = 8000
    left = np.concatenate([tone(1), np.zeros(3 * rate)])
    right = np.concatenate([np.zeros(2 * rate), tone(1), np.zeros(rate)])
    path = tmp_path / "rec.wav"
    path.write_bytes(wav_bytes([left, right], rate))

    class FakeSpeech:
        rate = 16000
        heard = iter(["Hello, is that Alex?", "Yes, speaking."])

        async def transcribe(self, pcm):
            return next(self.heard)

    z = zipfile.ZipFile(io.BytesIO(asyncio.run(recording_sample(str(path), FakeSpeech(), "Alex", ["caller", "me"]))))
    meta = json.loads(z.read("transcript.json"))
    assert [(line["who"], line["text"]) for line in meta["lines"]] == [
        ("caller", "Hello, is that <owner>?"), ("me", "Yes, speaking."),
    ]
    assert meta["channels"] == 2 and "recording.wav" in z.namelist()
