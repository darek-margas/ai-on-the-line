# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Darek Margas. See NOTICE.
"""Cuts the callee's audio into utterances using WebRTC VAD."""

from collections import deque

import numpy as np
import webrtcvad

from .audiosocket import FRAME_MS, frame_bytes

NOISE_MARGIN = 2.0  # ~6 dB above the line's background noise
MIN_SPEECH_RMS = 120.0  # about -48 dBFS: quieter than this is never speech
PREROLL_MS = 500  # audio kept from before speech was confirmed, so first syllables survive


class Segmenter:
    """Feed PCM in; get ("start", None) when speech begins and ("end", pcm)
    with the whole utterance once the speaker pauses. WebRTC VAD accepts 8,
    16, 32 and 48 kHz."""

    def __init__(self, rate: int = 8000, aggressiveness: int = 2, start_ms: int = 120, end_ms: int = 700, max_ms: int = 20000):
        self.vad = webrtcvad.Vad(aggressiveness)
        self.rate = rate
        self.frame_bytes = frame_bytes(rate)
        self.start_frames = start_ms // FRAME_MS
        self.end_frames = end_ms // FRAME_MS
        self.max_bytes = max_ms // FRAME_MS * self.frame_bytes
        self._buf = bytearray()
        # Sized for the longest start threshold, which callers may raise later.
        self._preroll: deque[bytes] = deque(maxlen=PREROLL_MS // FRAME_MS + 20)
        self._speech = bytearray()
        self._in_speech = False
        self._voiced_run = 0
        self._silent_run = 0
        self._floor: float | None = None  # background noise level (RMS)

    def _loud_enough(self, frame: bytes, vad_voiced: bool) -> bool:
        """Noise gate. WebRTC VAD alone calls a noisy mobile line 'speech', and
        Whisper then invents words from it. Track the line's noise floor (from
        frames VAD doesn't call speech, so speech and echo can't raise it) and
        only accept frames clearly above it."""
        samples = np.frombuffer(frame, dtype=np.int16).astype(np.float32)
        rms = float(np.sqrt(np.mean(samples * samples)))
        if rms < 2:
            return False  # digital silence (gap filler): not speech, not line noise either
        if self._floor is None:
            self._floor = MIN_SPEECH_RMS / NOISE_MARGIN
        if not vad_voiced:
            rate = 0.1 if rms < self._floor else 0.02
            self._floor += (rms - self._floor) * rate
        return rms > max(self._floor * NOISE_MARGIN, MIN_SPEECH_RMS)

    @property
    def in_speech(self) -> bool:
        return self._in_speech

    def push(self, pcm: bytes) -> list[tuple[str, bytes | None]]:
        events: list[tuple[str, bytes | None]] = []
        self._buf += pcm
        while len(self._buf) >= self.frame_bytes:
            frame = bytes(self._buf[: self.frame_bytes])
            del self._buf[: self.frame_bytes]
            vad_voiced = self.vad.is_speech(frame, self.rate)
            voiced = vad_voiced and self._loud_enough(frame, vad_voiced)
            if not vad_voiced:
                self._loud_enough(frame, False)  # keep learning the noise floor

            if not self._in_speech:
                self._preroll.append(frame)
                self._voiced_run = self._voiced_run + 1 if voiced else 0
                if self._voiced_run >= self.start_frames:
                    self._in_speech = True
                    self._silent_run = 0
                    self._speech = bytearray(b"".join(self._preroll))
                    self._preroll.clear()
                    events.append(("start", None))
                continue

            self._speech += frame
            self._silent_run = 0 if voiced else self._silent_run + 1
            if self._silent_run >= self.end_frames or len(self._speech) >= self.max_bytes:
                events.append(("end", bytes(self._speech)))
                self._in_speech = False
                self._voiced_run = 0
                self._speech = bytearray()
        return events
