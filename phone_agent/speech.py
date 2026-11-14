"""Speech-to-text and text-to-speech through Wyoming (faster-whisper and Piper containers)."""

import re

import numpy as np
import soxr
from wyoming.asr import Transcribe, Transcript
from wyoming.audio import AudioChunk, AudioStart, AudioStop
from wyoming.client import AsyncTcpClient
from wyoming.tts import Synthesize, SynthesizeVoice

STT_RATE = 16000
CACHE_MAX_TEXT = 120  # short, repeated phrases are worth caching


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
        self._tts_cache: dict[str, bytes] = {}

    def _spoken(self, text: str) -> str:
        for word, spoken in self.pronounce.items():
            text = re.sub(rf"\b{re.escape(word)}\b", spoken, text)
        return text

    def clear_cache(self) -> None:
        self._tts_cache.clear()

    async def transcribe(self, pcm: bytes) -> str:
        """Takes call audio at self.rate."""
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
            return self._tts_cache[text]
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
        pcm = resample(b"".join(chunks), rate or self.rate, self.rate)
        if len(text) <= CACHE_MAX_TEXT:
            self._tts_cache[text] = pcm
        return pcm
