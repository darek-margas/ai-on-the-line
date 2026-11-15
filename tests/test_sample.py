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


class FakeVoice:
    """Hears a fixed line per stretch of speech; 'speaks' a 300 Hz tone."""

    def __init__(self, heard, rate=8000):
        self.heard, self.rate, self.said = iter(heard), rate, []

    async def transcribe(self, pcm):
        return next(self.heard)

    async def synthesize(self, text):
        self.said.append(text)
        t = np.arange(int(self.rate * 0.8)) / self.rate
        return (np.sin(2 * np.pi * 300 * t) * 8000).astype(np.int16).tobytes()


def test_stretch_hits_exact_length():
    from phone_agent.sample import stretch

    voice = tone(0.8).astype(np.float32)
    for seconds in (0.4, 0.8, 2.0):
        assert len(stretch(voice, int(seconds * 8000), 8000)) == int(seconds * 8000)


def test_revoice_track_keeps_timing_not_voice():
    from phone_agent.sample import revoice_track, speech_segments

    rate = 8000
    original = np.concatenate([np.zeros(rate), tone(1.5), np.zeros(rate), tone(0.6), np.zeros(rate)]).astype(np.int16)
    speech = FakeVoice(["Hi, it's Alex, call me on 0491 570 156.", ""])
    out, lines = asyncio.run(revoice_track(original, rate, speech, ["Alex"]))
    assert len(out) == len(original)
    # Sound exactly where the original had speech, silence everywhere else.
    segments = speech_segments(original, rate)
    inside = np.zeros(len(out), dtype=bool)
    for a, b in segments:
        inside[int(a * rate) : int(b * rate)] = True
        assert np.abs(out[int(a * rate) : int(b * rate)]).mean() > 1000
    assert np.abs(out[~inside]).max() == 0
    # Not the original audio: the first stretch is the TTS tone, the second (nothing recognised) is noise.
    first = slice(int(1.1 * rate), int(2.3 * rate))
    assert abs(np.corrcoef(out[first].astype(float), original[first].astype(float))[0, 1]) < 0.2
    assert speech.said == ["Hi, it's Alex, call me on a number."]  # spoken redacted, owner as "Alex"
    assert lines == [{"t": 1.0, "text": "Hi, it's <owner>, call me on <number>."}]


def test_revoiced_call_sample(tmp_path):
    from phone_agent.sample import revoiced_call_sample

    rec = tmp_path / "phone-agent"
    rec.mkdir()
    (rec / "abc-rx.wav").write_bytes(wav_bytes([np.concatenate([np.zeros(8000), tone(1), np.zeros(8000)])]))
    (rec / "abc-tx.wav").write_bytes(wav_bytes([np.concatenate([tone(1), np.zeros(16000)])]))
    (rec / "abc.wav").write_bytes(wav_bytes([tone(3)]))
    call = {"id": "abc", "brief": {"direction": "outbound"}, "recording": "phone-agent/abc.wav",
            "transcript": [["agent", "Hello."], ["callee", "Hi."]], "timings": [0.0, 1.0]}
    voices = iter([FakeVoice(["Hi."]), FakeVoice(["Hello."])])
    z = zipfile.ZipFile(io.BytesIO(asyncio.run(revoiced_call_sample(call, "Alex", str(tmp_path), lambda rate: next(voices)))))
    assert sorted(z.namelist()) == ["README.txt", "agent.wav", "call.wav", "other.wav", "transcript.json", "transcript.txt"]
    meta = json.loads(z.read("transcript.json"))
    assert meta["revoiced"] is True and meta["format"] == "ai-on-the-line-sample/1"
    original = np.frombuffer(wave.open(str(rec / "abc-rx.wav")).readframes(10**6), dtype=np.int16)
    revoiced = np.frombuffer(wave.open(io.BytesIO(z.read("other.wav"))).readframes(10**6), dtype=np.int16)
    assert len(revoiced) == len(original) and not np.array_equal(revoiced, original)
