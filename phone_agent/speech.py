# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Darek Margas. See NOTICE.
"""Speech-to-text and text-to-speech through Wyoming (faster-whisper and Piper containers)."""

import asyncio
import re
from collections import OrderedDict

import numpy as np
import soxr
from wyoming.asr import Transcribe, Transcript
from wyoming.audio import AudioChunk, AudioStart, AudioStop
from wyoming.client import AsyncTcpClient
from wyoming.tts import Synthesize, SynthesizeVoice

STT_RATE = 16000
CACHE_MAX_TEXT = 120  # short, repeated phrases are worth caching
# Least recently used phrases go first past this: openings, fillers and notices
# are rendered before every call and stay; one-off model sentences age out.
CACHE_MAX_BYTES = 32 * 1024 * 1024
# A Whisper or Piper server that accepts the connection and then stops
# answering would otherwise hold the call (or an inbound slot) forever.
STT_TIMEOUT_SECONDS = 30
TTS_TIMEOUT_SECONDS = 30


class SpeechTimeout(ConnectionError):
    """The Wyoming server took the request but didn't answer in time."""


def resample(pcm: bytes, src_rate: int, dst_rate: int) -> bytes:
    if src_rate == dst_rate:
        return pcm
    samples = np.frombuffer(pcm, dtype=np.int16)
    return soxr.resample(samples, src_rate, dst_rate).astype(np.int16).tobytes()


class Speech:
    def __init__(self, stt: tuple[str, int], tts: tuple[str, int], language: str, voice: str, rate: int = 16000,
                 pronounce: dict[str, str] | None = None):
        self.stt = stt
        self.tts = tts
        self.language = language
        self.voice = voice
        self.rate = rate  # what the call audio runs at
        # Spellings that make the voice say a word right, e.g. {"Siobhan": "Shivawn"}
        # (Piper reads unusual names letter by letter or wrongly).
        self.pronounce = pronounce or {}
        self._tts_cache: OrderedDict[str, bytes] = OrderedDict()
        self._cache_bytes = 0

    def _spoken(self, text: str) -> str:
        for word, spoken in self.pronounce.items():
            text = re.sub(rf"\b{re.escape(word)}\b", spoken, text)
        return text

    def clear_cache(self) -> None:
        self._tts_cache.clear()
        self._cache_bytes = 0

    async def transcribe(self, pcm: bytes) -> str:
        """Takes call audio at self.rate."""
        try:
            return await asyncio.wait_for(self._transcribe(pcm), STT_TIMEOUT_SECONDS)
        except TimeoutError:
            raise SpeechTimeout(f"Wyoming STT {self.stt[0]}:{self.stt[1]} didn't answer in {STT_TIMEOUT_SECONDS} s")

    async def _transcribe(self, pcm: bytes) -> str:
        pcm = resample(pcm, self.rate, STT_RATE)
        fmt = {"rate": STT_RATE, "width": 2, "channels": 1}
        async with AsyncTcpClient(*self.stt) as client:
            await client.write_event(Transcribe(language=self.language).event())
            await client.write_event(AudioStart(**fmt).event())
            for i in range(0, len(pcm), 6400):
                await client.write_event(AudioChunk(audio=pcm[i : i + 6400], **fmt).event())
            await client.write_event(AudioStop().event())
            while True:
                event = await client.read_event()
                if event is None:
                    raise ConnectionError("Wyoming STT closed the connection")
                if Transcript.is_type(event.type):
                    return Transcript.from_event(event).text.strip()

    async def synthesize(self, text: str) -> bytes:
        """Returns 16-bit mono PCM at self.rate, ready for AudioSocket."""
        if text in self._tts_cache:
            self._tts_cache.move_to_end(text)
            return self._tts_cache[text]
        try:
            pcm = await asyncio.wait_for(self._synthesize(text), TTS_TIMEOUT_SECONDS)
        except TimeoutError:
            raise SpeechTimeout(f"Wyoming TTS {self.tts[0]}:{self.tts[1]} didn't answer in {TTS_TIMEOUT_SECONDS} s")
        if len(text) <= CACHE_MAX_TEXT and text not in self._tts_cache:
            self._tts_cache[text] = pcm
            self._cache_bytes += len(pcm)
            while self._cache_bytes > CACHE_MAX_BYTES and len(self._tts_cache) > 1:
                _, old = self._tts_cache.popitem(last=False)
                self._cache_bytes -= len(old)
        return pcm

    async def _synthesize(self, text: str) -> bytes:
        voice = SynthesizeVoice(name=self.voice) if self.voice else None
        chunks: list[bytes] = []
        rate = None
        async with AsyncTcpClient(*self.tts) as client:
            await client.write_event(Synthesize(text=self._spoken(text), voice=voice).event())
            while True:
                event = await client.read_event()
                if event is None:
                    raise ConnectionError("Wyoming TTS closed the connection")
                if AudioChunk.is_type(event.type):
                    chunk = AudioChunk.from_event(event)
                    if chunk.width != 2 or chunk.channels != 1:
                        raise ValueError(f"unexpected TTS audio format {chunk.width=} {chunk.channels=}")
                    rate = chunk.rate
                    chunks.append(chunk.audio)
                elif AudioStop.is_type(event.type):
                    break
        return resample(b"".join(chunks), rate or self.rate, self.rate)
