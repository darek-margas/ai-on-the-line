# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Darek Margas. See NOTICE.
"""How well your Whisper hears short answers on a phone line, and how fast.

Short phone answers ("Eight.", "Yes, it does.") are where speech recognition
fails first: there's little context, and an 8 kHz line removes the upper half
of the voice. This renders a set of them with your Piper, squeezes them through
phone bandwidth, sends them to your Whisper and counts how many it got right
("3" for "Three" or "$55" for "fifty five dollars" counts as right).

Run it inside the agent's container, so it uses the same settings (.env):

    docker compose exec -T phone-agent python - < tools/stt_bench.py

Change the model on your Whisper server, run it again, compare.
"""

import asyncio
import re
import time

from phone_agent.config import Config
from phone_agent.speech import Speech, resample

PHRASES = [
    "Eight.", "Three.", "Seven.", "Nine.", "Two.", "Yes, it does.", "No, that is fine.",
    "Twenty five litres and nine dollars.", "A hundred and twenty for one cubic metre.",
    "Fifty five dollars for delivery.", "Landscape, John speaking.",
]


ONES = "zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen " \
       "seventeen eighteen nineteen".split()
TENS = "_ _ twenty thirty forty fifty sixty seventy eighty ninety".split()


def spoken(n: int) -> str:
    if n < 20:
        return ONES[n]
    if n < 100:
        return TENS[n // 10] + (" " + ONES[n % 10] if n % 10 else "")
    return ONES[n // 100] + " hundred" + (" and " + spoken(n % 100) if n % 100 else "")


def same(said: str, heard: str) -> bool:
    """Equal in words: "3." matches "Three.", "$55" matches "fifty five
    dollars", "meter" matches "metre". Punctuation and case don't count."""
    def norm(text: str) -> str:
        text = text.lower().replace("meter", "metre")
        text = re.sub(r"\$(\d+)", r"\1 dollars", text)
        text = re.sub(r"\d+", lambda m: spoken(int(m.group())) if int(m.group()) < 1000 else m.group(), text)
        text = re.sub(r"\ba hundred\b", "one hundred", text)
        return " ".join(re.sub(r"[^a-z0-9 ]", " ", text).replace("-", " ").split())
    return norm(said) == norm(heard)


async def main() -> None:
    cfg = Config.from_env()
    speech = Speech(cfg.stt, cfg.tts, cfg.stt_language, cfg.tts_voice, 16000)
    ok, times = 0, []
    for phrase in PHRASES:
        pcm = await speech.synthesize(phrase)
        phone = resample(resample(pcm, 16000, 8000), 8000, 16000)  # through phone bandwidth
        start = time.monotonic()
        heard = await speech.transcribe(phone)
        times.append(time.monotonic() - start)
        good = same(phrase, heard)
        ok += good
        print(f"  {'OK ' if good else 'BAD'} {phrase!r:45} -> {heard!r}")
    print(f"{ok}/{len(PHRASES)} right, median {sorted(times)[len(times) // 2] * 1000:.0f} ms")


asyncio.run(main())
