# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Darek Margas. See NOTICE.
"""Test samples: a call's audio and transcript in one zip, for tuning and tests.

Turn-taking problems only show up against real voices: people who say "Ah."
and keep going, voice robots that pause mid-sentence, hold music. A sample
keeps the other side's audio on its own, so it can be played back into the
agent in a test, with the transcript to check against.

Two sources:
- a call the agent made or answered (the call page's "Test sample" link, or
  python -m phone_agent.sample --call <id>), with each side on its own when
  the PBX recordings are mounted (RECORDINGS_DIR);
- any other recording from the PBX, e.g. a call you took yourself
  (python -m phone_agent.sample --recording <file.wav>): transcribed here with
  Whisper, one line per stretch of speech.

Text is redacted (numbers, email addresses, references, the owner's name).
Audio isn't: listen before you share a sample.
"""

import argparse
import asyncio
import io
import json
import os
import re
import sys
import wave
import zipfile
from datetime import datetime, timezone

import numpy as np
import webrtcvad

from .credit import PROJECT, PROJECT_URL

FORMAT = "ai-on-the-line-sample/1"

README = f"""A test sample from {PROJECT} ({PROJECT_URL}).

Files
  other.wav        the other side only (callee or caller), as the PBX received it
  agent.wav        the agent only, as sent to the line
  call.wav         both sides mixed (levelled if the PBX levels recordings)
  recording.wav    a recording made outside the agent (both sides, as recorded)
  transcript.json  lines with time in seconds from the start of the recording
  transcript.txt   the same, for reading

Only the files that were available are included.

Times are approximate (within about a second): for the agent's calls they're
when a line was heard or started; for other recordings, when a stretch of
speech started. Use the audio for exact timing.

The text has numbers, email addresses, long references and the owner's name
replaced with <number>, <email>, <ref> and <owner>. The audio is not redacted.
Listen to all of it before you share it, and only share calls where the other
person agreed to be recorded or where you're allowed to publish the recording.

If you contribute this sample to the project, it's shared under CC BY-SA 4.0.
"""

EMAIL = re.compile(r"[\w.+-]+@[\w-]+(\.[\w-]+)+")
NUMBER = re.compile(r"\+?\d[\d \-()]{4,}\d")
REF = re.compile(r"\b(?=[A-Za-z0-9-]*\d)(?=[A-Za-z0-9-]*[A-Za-z])[A-Za-z0-9-]{6,}\b")
SPELT = re.compile(r"\b(?:[A-Z]\s){3,}[A-Z]\b")  # "B B A K Q" read out letter by letter


def redact(text: str, names: list[str] = ()) -> str:
    text = EMAIL.sub("<email>", text)
    text = NUMBER.sub("<number>", text)
    text = REF.sub("<ref>", text)
    text = SPELT.sub("<ref>", text)
    for name in names:
        if name:
            text = re.sub(rf"\b{re.escape(name)}\b", "<owner>", text, flags=re.IGNORECASE)
    return text


def owner_names(owner: str) -> list[str]:
    """The full name and each part of it, longest first."""
    parts = [p for p in owner.split() if len(p) > 1]
    return sorted({owner, *parts}, key=len, reverse=True) if owner else []


def _text_files(meta: dict, lines: list[dict]) -> dict[str, bytes]:
    meta = {"format": FORMAT, **meta, "lines": lines}
    def stamp(t):
        return f"[{t:7.1f}]" if t is not None else "[      ?]"

    txt = "\n".join(f"{stamp(line['t'])} {line['who']}: {line['text']}" for line in lines)
    return {
        "README.txt": README.encode(),
        "transcript.json": json.dumps(meta, indent=2, ensure_ascii=False).encode(),
        "transcript.txt": (txt + "\n").encode(),
    }


def _zip(files: dict[str, bytes]) -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in files.items():
            z.writestr(name, data)
    return out.getvalue()


def call_sample(call: dict, owner: str, recordings_dir: str = "") -> bytes:
    """A zip for a call from the call database."""
    names = owner_names(owner)
    inbound = (call.get("brief") or {}).get("direction") == "inbound"
    times = call.get("timings") or []
    lines = []
    for i, (who, text) in enumerate(call.get("transcript") or []):
        lines.append({
            "t": round(times[i], 1) if i < len(times) and times[i] is not None else None,
            "who": "agent" if who == "agent" else ("caller" if inbound else "callee"),
            "text": redact(text, names),
        })
    # A callee line is stamped when they started speaking, so it can come before
    # an agent line that was logged earlier.
    if all(line["t"] is not None for line in lines):
        lines.sort(key=lambda line: line["t"])
    files = {}
    if recordings_dir and call.get("recording"):
        base = os.path.join(recordings_dir, call["recording"])[: -len(".wav")]
        # MixMonitor runs on the line's channel: r() is what came from the other
        # side, t() what we sent to it.
        for name, path in (("other.wav", base + "-rx.wav"), ("agent.wav", base + "-tx.wav"), ("call.wav", base + ".wav")):
            if os.path.isfile(path):
                with open(path, "rb") as f:
                    files[name] = f.read()
    meta = {
        "source": "agent call",
        "direction": "inbound" if inbound else "outbound",
        "outcome": call.get("outcome"),
        "status": call.get("status"),
        "date": _day(call.get("started") or call.get("created")),
        "duration": round(call["ended"] - call["started"], 1) if call.get("ended") and call.get("started") else None,
        "audio": sorted(files),
    }
    return _zip({**_text_files(meta, lines), **files})


def _day(ts: float | None) -> str | None:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d") if ts else None


def _read_wav(path: str) -> tuple[list[np.ndarray], int]:
    with wave.open(path) as w:
        if w.getsampwidth() != 2:
            raise ValueError(f"{path}: only 16-bit PCM WAV is supported (convert with sox or ffmpeg)")
        rate, channels = w.getframerate(), w.getnchannels()
        data = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
    return [data[c::channels] for c in range(channels)], rate


def speech_segments(pcm: np.ndarray, rate: int, min_silence: float = 0.6) -> list[tuple[float, float]]:
    """(start, end) seconds of each stretch of speech, split at pauses of
    min_silence or longer."""
    vad = webrtcvad.Vad(2)
    vad_rate = rate if rate in (8000, 16000, 32000, 48000) else 16000
    if vad_rate != rate:
        import soxr
        pcm = soxr.resample(pcm.astype(np.float32), rate, vad_rate).clip(-32768, 32767).astype(np.int16)
    frame = vad_rate * 30 // 1000
    segments, start, last = [], None, 0.0
    for i in range(0, len(pcm) - frame + 1, frame):
        t = i / vad_rate
        if vad.is_speech(pcm[i : i + frame].tobytes(), vad_rate):
            if start is None:
                start = t
            last = t + 0.03
        elif start is not None and t - last >= min_silence:
            if last - start >= 0.3:
                segments.append((start, last))
            start = None
    if start is not None and last - start >= 0.3:
        segments.append((start, last))
    return segments


async def recording_sample(path: str, speech, owner: str = "", who: list[str] | None = None) -> bytes:
    """A zip for a recording made outside the agent: each stretch of speech
    transcribed with Whisper. A stereo file is taken as one side per channel."""
    channels, rate = _read_wav(path)
    who = who or (["line"] if len(channels) == 1 else [f"channel {c + 1}" for c in range(len(channels))])
    names = owner_names(owner)
    speech.rate = rate
    lines = []
    for c, pcm in enumerate(channels):
        for start, end in speech_segments(pcm, rate):
            text = await speech.transcribe(pcm[int(start * rate) : int(end * rate)].tobytes())
            if text:
                lines.append({"t": round(start, 1), "who": who[c] if c < len(who) else f"channel {c + 1}",
                              "text": redact(text, names)})
    lines.sort(key=lambda line: line["t"])
    with open(path, "rb") as f:
        audio = f.read()
    meta = {
        "source": "PBX recording",
        "file": os.path.basename(path),
        "duration": round(len(channels[0]) / rate, 1),
        "channels": len(channels),
        "audio": ["recording.wav"],
    }
    return _zip({**_text_files(meta, lines), "recording.wav": audio})


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m phone_agent.sample",
        description="Export a call's audio and transcript as a test sample (zip). Reads the same settings (.env) as the server.",
    )
    src = parser.add_mutually_exclusive_group(required=True)
    src.add_argument("--call", help="call id (or its first characters) from the agent's call history")
    src.add_argument("--recording", help="a 16-bit WAV from the PBX, e.g. under /var/spool/asterisk/monitor")
    parser.add_argument("--who", help="names for the channels of --recording, comma separated, e.g. caller,me")
    parser.add_argument("-o", "--output", help="zip file to write (default: sample-<id or file name>.zip)")
    args = parser.parse_args(argv)

    from .config import Config
    from .db import Database
    from .speech import Speech

    cfg = Config.from_env()
    if args.call:
        db = Database(cfg.data_dir)
        matches = [c for c in db.recent_calls(10000) if c["id"].startswith(args.call)]
        if len(matches) != 1:
            print(f"{len(matches)} calls match {args.call!r}", file=sys.stderr)
            return 1
        call = db.get_call(matches[0]["id"])
        data = call_sample(call, cfg.owner_name, cfg.recordings_dir)
        out = args.output or f"sample-{call['id'][:8]}.zip"
    else:
        path = args.recording
        if not os.path.isfile(path) and cfg.recordings_dir:
            path = os.path.join(cfg.recordings_dir, args.recording)
        speech = Speech(cfg.stt, cfg.tts, cfg.stt_language, cfg.tts_voice)
        who = args.who.split(",") if args.who else None
        data = asyncio.run(recording_sample(path, speech, cfg.owner_name, who))
        out = args.output or f"sample-{os.path.splitext(os.path.basename(path))[0]}.zip"
    with open(out, "wb") as f:
        f.write(data)
    print(f"wrote {out} ({len(data) // 1024} KB). Listen to the audio before you share it: only the text is redacted.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
