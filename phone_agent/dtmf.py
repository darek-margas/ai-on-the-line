"""Keypad tones (DTMF) generated as audio, like a phone pressing keys.

ARI can only send DTMF to channels it controls, so the agent plays the
tones in-band; over G.711 trunks the far end's phone menu hears them.
"""

import numpy as np

FREQS = {
    "1": (697, 1209), "2": (697, 1336), "3": (697, 1477),
    "4": (770, 1209), "5": (770, 1336), "6": (770, 1477),
    "7": (852, 1209), "8": (852, 1336), "9": (852, 1477),
    "*": (941, 1209), "0": (941, 1336), "#": (941, 1477),
}
TONE_MS = 150
GAP_MS = 100
LEVEL = 0.35  # of full scale for each of the two tones; well clear of clipping


def tones(digits: str, rate: int) -> bytes:
    """16-bit mono PCM for the digits, with a short gap after each."""
    t = np.arange(int(rate * TONE_MS / 1000)) / rate
    gap = np.zeros(int(rate * GAP_MS / 1000))
    parts = []
    for digit in digits:
        if digit not in FREQS:
            continue
        low, high = FREQS[digit]
        tone = LEVEL * (np.sin(2 * np.pi * low * t) + np.sin(2 * np.pi * high * t))
        parts += [tone, gap]
    if not parts:
        return b""
    return (np.concatenate(parts) * 32767 * 0.9).astype(np.int16).tobytes()
