# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Darek Margas. See NOTICE.
"""One live conversation over an AudioSocket connection.

Three things run at once: a reader that feeds the callee's audio through VAD,
a sender that plays our speech at real-time pace, and the conversation loop
that turns utterances into transcripts, LLM turns and speech.
"""

import asyncio
import logging
import random
import re
import time
from collections import deque
from typing import TYPE_CHECKING

import numpy as np
import soxr

from . import audiosocket as ass
from . import dtmf
from .dialog import Dialog, Turn, says_goodbye
from .speech import Speech
from .vad import Segmenter

if TYPE_CHECKING:
    from .calls import CallState

log = logging.getLogger(__name__)

HANGUP = object()
SUPERSEDED = object()  # a reply dropped because they carried on before it started
LISTEN_SECONDS = 10
SILENT_ANSWER_SECONDS = 5  # answered but nobody speaks: introduce ourselves
ECHO_OVERLAP = 0.6  # share of heard words that must be ours to count as echo
INTRO_PAUSE_SECONDS = 2.5  # quiet after our introduction before we just carry on
# Extra wait when their sentence sounds unfinished: longer when it clearly trails
# off ("I think about...", "...and the"), shorter when Whisper just left out the
# full stop, which it often does on a complete sentence.
CONTINUE_SECONDS = 1.5
TRAILING_OFF_SECONDS = 2.5
MID_SENTENCE_MAX_SECONDS = 25  # longest we wait for a sentence they're in the middle of (VAD cuts at 20 s)
LONE_THANKS_SECONDS = 10.0  # a bare "Thank you." later than this after our question is Whisper hearing noise
AFTER_GOODBYE_SECONDS = 2.0  # listen after our goodbye before hanging up
THINKING_SECONDS = 20  # quiet after our question is them thinking, not a reason to re-ask
OVERLAP_MIN_SECONDS = 1.0  # speech starting with less than this of our line left is an answer
INBOUND_GREETING_DELAY_SECONDS = 0.5  # answering: pause before "Hi, you've reached..."
STALE_HELLO_SECONDS = 4.0  # a bare "hello?" this soon after we started talking is already answered
# Reply not ready this long after they spoke: say "Let me think." Never two turns
# in a row: a person says it now and then, not before every answer. With the
# reply streaming, real words usually start within 1-1.8 s; a filler at 1 s
# landed right where people carry on with their sentence.
FILLER_AFTER_SECONDS = 1.8
# Pre-rendered before dialing. Short, so the reply isn't held up behind them; no
# "Mm-hm.": Piper reads it almost letter by letter ("em-em-hm").
FILLERS = ["Let me think.", "Let me see.", "Hmm, let me see."]
END_OF_SPEECH_MS = 700  # pause that ends the callee's turn (550 split real sentences)
INTRO_EVENT = (
    "You've just said your introduction (the last 'You:' line) and they're quietly listening. "
    "Go straight into your first question as a continuation of it: no greeting, no 'thanks for "
    "picking up', no 'great'. For example: 'So, first: do you sell large outdoor pots?'"
)
MIN_UTTERANCE_MS = 300  # shorter blips are usually noise
# Speech has to last longer to count as an interruption while we're talking,
# so line echo of our own voice doesn't cut us off.
START_MS_IDLE = 120
START_MS_SPEAKING = 300
# Speech this long while we're paused is a real interruption, whatever it says.
BARGE_IN_CONFIRM_SECONDS = 1.5


HOW_ARE_YOU_REPLY = "I'm good, thanks for asking!"
RECORDING_HINT = re.compile(
    r"\bpress\b|thank(s| you) for calling|welcome to|your call is|open until|opening hours|"
    r"please (hold|wait|stay)|voicemail|leave a message|not available|after the (tone|beep)",
    re.I,
)
PERSON_CUE = re.compile(
    r"\bspeaking\b|^\W*hello\W*$|hello\?|how can i help|can i help|say (it|that) again|are you there|"
    r"anyone there|who'?s (this|calling)|good (morning|afternoon)|direct your call|how may i help",
    re.I,
)
BARE_HELLO = re.compile(r"^\W*(hello|hi|hey)\W*$", re.I)
HOW_ARE_YOU = re.compile(r"how are (you|ya)|how('s| is) it going|how('re| are) things|how you going", re.I)


ACKNOWLEDGEMENT = re.compile(
    r"^(?:(?:okay|ok|yeah|yep|yes|sure|mm+|mhm|uh[- ]?huh|right|go ahead|go on|alright|all right|fire away|shoot|"
    r"no worries|thanks|thank you|cheers|great|good|perfect|lovely|cool|"
    r"(?:it'?s|that'?s|all) (?:fine|good|ok|okay|all good))[\s,.!]*)+$",
    re.I,
)


# Only a reply that is nothing but a farewell closes the call after our goodbye.
# "Thanks, but before you go, the price excludes delivery" is not one.
FAREWELL_ONLY = re.compile(
    r"^(?:(?:ok(?:ay)?|alright|all right|yeah|yep|no worries|no problem|you too|and you|"
    r"thanks|thank you|thanks a lot|cheers|ta|bye|bye bye|goodbye|see ya|see you|take care|"
    r"have a (?:good|nice|great) (?:one|day|night|afternoon|evening|weekend)|you're welcome)[\s,.!]*)+$",
    re.I,
)
TRAILING_WORDS = set(
    "and or but so because which that who when if then to the a an of for with after before "
    "on in at as like my your our their is was are were".split()
)


# "Ah." / "Um." on its own: they're about to say something, not answering.
HESITATION = re.compile(r"^\W*(?:(?:ah+|um+|uh+|er+|erm|hmm+|well)[\s,.!?…]*)+$", re.I)


def asks_us(text: str) -> bool:
    """'You're looking for the manager or...': an open question to us, answer it."""
    return bool(re.search(r"\bor\s*(?:\.\.\.|…|\?)$", text.strip(), re.I))


def trails_off(text: str) -> bool:
    """'I could... I think about...' / 'a quote for the' / 'Ah.': clearly mid-thought."""
    text = text.strip()
    if HESITATION.match(text):
        return True
    words = re.findall(r"[a-z']+", text.lower())
    return text.endswith(("...", "…")) or (bool(words) and words[-1] in TRAILING_WORDS)


def sounds_unfinished(text: str) -> bool:
    """Whisper ends complete sentences with . ? or !; a trailing comma, no
    punctuation, or a dangling 'and'/'the' means they're still going."""
    text = text.strip()
    if not text or asks_us(text):
        return False
    if HESITATION.match(text):
        return True
    if text.endswith(("...", "…")) or not text.endswith((".", "?", "!")):
        return True
    words = re.findall(r"[a-z']+", text.lower())
    return bool(words) and words[-1] in TRAILING_WORDS


LISTENING = re.compile(
    r"^(?:(?:okay|ok|yeah|yep|sure|mm+|mhm|uh[- ]?huh|go ahead|go on|alright|all right|fire away|shoot)[\s,.!]*)+$",
    re.I,
)


def is_listening(text: str) -> bool:
    """'Okay, go ahead.': they're waiting for us, not answering."""
    return bool(LISTENING.match(text.strip()))


# A bare thank-you right after our question: often Whisper's version of a short
# answer like "three". "That's fine" / "great" are real answers and don't count.
THANKS_ONLY = re.compile(r"^(?:(?:thanks|thank you|cheers)[\s,.!]*)+$", re.I)


def is_acknowledgement(text: str) -> bool:
    """'Okay, go ahead.' / 'Yeah, sure.': listening, not an answer."""
    return bool(ACKNOWLEDGEMENT.match(text.strip()))


def split_sentences(text: str) -> list[str]:
    """'Got it, thanks. Next one: what colour?' -> ['Got it, thanks.', 'Next one: what colour?']"""
    parts = [p.strip() for p in re.split(r"(?<=[.!?])\s+", text.strip())]
    return [p for p in parts if p] or [text]


def opening_lines(opening: str) -> list[str]:
    """The opening as-is, and with a reply to 'how are you' in front. Both are
    pre-rendered before dialing so a person answering hears us at once."""
    return [opening, f"{HOW_ARE_YOU_REPLY} {opening}"]


SOUND_WORDS = {"beep", "beeps", "bleep", "ring", "ringing", "rings", "music", "tone", "tones", "ding", "dong", "click", "buzz", "silence", "you"}


# Queue announcements, in the words Australian call centres use.
QUEUE_PHRASE = (
    r"your call is important|in the queue|you are (caller )?number \w+|(stay|remain) on the line|"
    r"all (of )?our [\w ]{0,30}?are (currently )?(busy|assisting|helping|on other calls|unavailable)|"
    r"answered in the order|next available|(estimated|expected|current) wait(ing)? time|"
    r"with you shortly|calls (may|are) (be )?(monitored|recorded)|apologi[sz]e for the (wait|delay)"
)
HOLD_PHRASE = re.compile(
    r"please (hold|wait)|\bhold (on|the line)\b|transferring|putting you through|connecting you|"
    r"one moment|just a moment|" + QUEUE_PHRASE,
    re.I,
)
# A repeated recording is only skipped when it's long enough to be sure, and
# never when it's a menu: a menu repeats because it wants an answer.
REPEAT_MIN_WORDS = 8
REPEAT_MATCH = 0.7
MENU_WORDS = re.compile(r"\bpress\b|\boption\b|\bsay\b|\bdial\b|\benter\b|\bkey\b", re.I)


def _words(text: str) -> list[str]:
    return re.findall(r"[a-z0-9']+", text.lower())


def is_repeat(text: str, earlier: list[str]) -> bool:
    """A hold message playing again: most of these words, in order, were already
    heard. A person doesn't say the same long sentence twice word for word; a
    hold loop always does, though speech recognition may split it differently."""
    words = _words(text)
    if len(words) < REPEAT_MIN_WORDS or MENU_WORDS.search(text):
        return False
    history = [w for line in earlier for w in _words(line)]
    if len(history) < REPEAT_MIN_WORDS:
        return False
    # Count the words covered by three-word runs heard before. Runs, because
    # short common words match anywhere; any order, because the loop may be
    # picked up mid-message and split differently each time.
    seen = {tuple(history[i:i + 3]) for i in range(len(history) - 2)}
    covered = [False] * len(words)
    for i in range(len(words) - 2):
        if tuple(words[i:i + 3]) in seen:
            covered[i:i + 3] = [True] * 3
    return sum(covered) / len(words) >= REPEAT_MATCH


# Promotions played in a queue, often phrased as questions that aren't for us.
PROMO_PHRASE = re.compile(
    r"did you know (that )?(we|you can|our|now)|have you (seen|tried|heard about|checked out) our|"
    r"(visit|check out|download|try|find us on) (our|the) [\w ]{0,20}?(website|app|online)|"
    r"\bwhy not\b|don'?t forget (to|that)|(we|now) (now )?(also )?(sell|stock|offer) |"
    r"you can (now |also )+\w+|(new|latest|special|great) (offers?|deals?|range|products?)|"
    r"\b(go to|head to|visit) \w+\.(com|net|org)|terms and conditions apply",
    re.I,
)
# What Whisper writes for music it can't make out: these are hold music, not a person.
MUSIC_HALLUCINATION = re.compile(r"thanks? (you )?for watching|subtitles by|please subscribe|\[?music\]?", re.I)


def is_hold_sound(text: str) -> bool:
    """'Beep. Beep.' / 'Music.' / '♪♪': Whisper's name for hold tones and music, not words."""
    if text.strip() and not re.search(r"[a-z0-9]", text, re.I):
        return True  # only symbols, e.g. "♪ ♪"
    words = re.findall(r"[a-z0-9']+", text.lower())
    return bool(words) and all(w in SOUND_WORDS for w in words)


RECORDING_MIN_WORDS = 15


def sounds_recorded(text: str) -> bool:
    """In a queue, a long polished line with no one introducing themselves is a
    recording. People pick up short: "Hello?", "Sam speaking", the business name.
    Menus are excluded: they want an answer."""
    return (len(_words(text)) >= RECORDING_MIN_WORDS and not PERSON_CUE.search(text)
            and not MENU_WORDS.search(text))


def is_promo(text: str) -> bool:
    """A recorded promotion ("Did you know we now sell tyres?"), with nothing in
    it that sounds like a person picking up."""
    return bool(PROMO_PHRASE.search(text)) and not PERSON_CUE.search(text)


def answered_after_menu(text: str) -> bool:
    """After our key press: a short phrase that isn't menu or hold-message talk
    is someone answering ("Whitegoods, Sam speaking", or a garbled store name)."""
    words = re.findall(r"[a-z0-9']+", text.lower())
    if not 1 <= len(words) <= 8 or THANKS_ONLY.match(text.strip()):
        return False
    if all(w in SOUND_WORDS for w in words):
        return False  # Whisper's name for a ringtone or hold music ("Beep."), not a person
    return not (RECORDING_HINT.search(text) or re.search(r"\b(press|option|hold|transfer|connecting|queue|menu)\b", text, re.I))


def person_greeting(text: str, seconds: float) -> bool:
    """Someone picked up: a short first utterance, or a longer reception-style
    greeting ("Good morning, Harvey Norman, Josh speaking, how may I direct your
    call?"), as long as it has no recording phrases."""
    if RECORDING_HINT.search(text):
        return False
    return seconds <= 4.0 or (seconds <= 10.0 and bool(PERSON_CUE.search(text)))


def asks_how_we_are(text: str) -> int:
    return 1 if HOW_ARE_YOU.search(text) else 0


def _words(text: str) -> list[str]:
    return re.findall(r"[a-z0-9']+", text.lower())


def is_echo(heard: str, our_line: str) -> bool:
    """Phone lines can return our own voice; speech recognition then 'hears'
    our words as the callee's. Treat it as echo when most of what was heard
    is words from the line we were saying at the time."""
    heard_words = _words(heard)
    ours = set(_words(our_line))
    if not heard_words or not ours:
        return False
    return sum(w in ours for w in heard_words) / len(heard_words) >= ECHO_OVERLAP


class CallSession:
    def __init__(
        self,
        call: "CallState",
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        speech: Speech,
        dialog: Dialog,
        *,
        max_call_seconds: int,
        hold_timeout_seconds: int,
        barge_in: bool,
        send_dtmf=None,
        max_hold_seconds: int = 1800,
    ):
        self.call = call
        self.send_dtmf = send_dtmf  # real keypad events (AMI); in-band tones if None
        self.reader = reader
        self.writer = writer
        self.speech = speech
        self.dialog = dialog
        self.max_call_seconds = max_call_seconds
        self.hold_timeout_seconds = hold_timeout_seconds
        # Time on hold doesn't count against max_call_seconds; this caps it instead.
        self.max_hold_seconds = max_hold_seconds
        self._hold_started: float | None = None
        self._held_seconds = 0.0
        # What they've said since we last spoke, to recognise a looping hold message.
        self._heard_since_we_spoke: list[str] = []
        self.barge_in = barge_in

        # Everything inside the session runs at the speech rate; Asterisk converts
        # to whatever codec the far end uses.
        self.rate = speech.rate
        self.frame_bytes = ass.frame_bytes(self.rate)
        self.silence = bytes(self.frame_bytes)
        self.out_kind = ass.RATE_KINDS[self.rate]
        self.segmenter = Segmenter(rate=self.rate, start_ms=START_MS_IDLE, end_ms=END_OF_SPEECH_MS)
        self.utterances: asyncio.Queue = asyncio.Queue()
        self.hung_up = False
        self._frames: deque[bytes] = deque()
        self._drained = asyncio.Event()
        self._drained.set()
        self._in_rate: int | None = None
        self._resampler: soxr.ResampleStream | None = None
        self._audio_clock = time.monotonic()  # how far the VAD has been fed
        # Barge-in: playback pauses when the callee seems to speak, and resumes
        # if it turns out to be noise (nothing transcribed).
        self._paused_at: float | None = None
        self._interruptible = True
        self._speaking_line = ""  # what we're saying now (for the echo check)
        self._overlapped_line: str | None = None
        self._heard_over_us = False
        self._last_filler = ""
        self._filled_last_turn = False
        self._last_said_at = 0.0
        # Put on hold or being transferred: hold tones get no reply and no filler,
        # and the next person to speak gets our introduction at once.
        self._on_hold_flag = False
        self._farewell_said = False  # incoming calls: we said goodbye and wait for them to hang up
        self._streamed_at = 0.0  # when the current reply's first streamed sentence went out
        # They often pause mid-thought ("...back in two weeks." ... "so I can't help
        # you."). If they carry on before our reply has started, it's dropped and
        # one reply covers everything they said.
        self._pending_turn: asyncio.Task | None = None
        self._superseded = False
        self._superseded_event: str | None = None

    async def run(self) -> None:
        tasks = [
            asyncio.create_task(self._read_loop()),
            asyncio.create_task(self._send_loop()),
            asyncio.create_task(self._gap_filler()),
        ]
        try:
            await self._converse()
            if not self.hung_up:
                await self._wait_drained(timeout=15)
        except Exception as exc:
            log.exception("call %s failed", self.call.id)
            self.call.error = repr(exc)
        finally:
            if not self.hung_up:
                try:
                    self.writer.write(ass.encode(ass.KIND_HANGUP))
                    await self.writer.drain()
                except ConnectionError:
                    pass
            for task in tasks:
                task.cancel()
            self.writer.close()

    # Conversation

    async def _converse(self) -> None:
        call = self.call
        started = time.monotonic()

        inbound = call.brief.direction == "inbound"
        if inbound:
            # We picked up: greet (pre-rendered) after a short beat, like a person
            # lifting the receiver, then it's their turn.
            await asyncio.sleep(INBOUND_GREETING_DELAY_SECONDS)
            await self._say(call.brief.opening_line)
            first = None
        else:
            # Listen first. A person says "Flower Power, Jordan speaking"; a recorded
            # greeting or menu can talk for a long time. Introduce ourselves into
            # silence, or straight away (pre-rendered, no LLM) when a person answers;
            # otherwise the dialog decides (recording: wait, voice menu: answer it).
            first = await self._first_words()
            if first is HANGUP:
                call.set_outcome(self._hangup_outcome())
                return
            if first is None:
                await self._say(call.brief.opening_line)
                if await self._follow_intro():
                    return
            else:
                self.utterances.put_nowait(first)

        silent_for = 0
        answered = first is None
        pending_event = None
        while True:
            if self._on_hold and time.monotonic() - self._hold_started >= self.max_hold_seconds:
                log.info("call %s: on hold for %d s, giving up", call.id, self.max_hold_seconds)
                call.notes.append(f"Gave up after {self.max_hold_seconds // 60} minutes on hold.")
                call.set_outcome("time_limit")
                return
            # Time on hold is added back: a long queue mustn't use up the conversation.
            remaining = self.max_call_seconds + self._hold_total() - (time.monotonic() - started)
            if remaining <= 0:
                if inbound:
                    await self._say(f"Sorry, I have to go now. I'll pass all this on to {call.owner}. Bye!")
                else:
                    await self._say(f"I'm sorry, I have to go now. Thank you for your help, goodbye.")
                call.set_outcome("time_limit")
                return

            they_spoke = False  # a filler only fits right after they've said something
            if pending_event:
                event, pending_event = pending_event, None
                item, forced = None, True
            else:
                item = await self._next_utterance(timeout=min(LISTEN_SECONDS, remaining))
                forced = False
            if not answered and item not in (None, HANGUP):
                answered = True
                pcm = item[0]
                text = await self._heard(item)
                if text and person_greeting(text, len(pcm) / (self.rate * 2)):
                    # Plain "Hello?" / "Flower Power, Jordan speaking": answer at once.
                    await self._say(opening_lines(call.brief.opening_line)[asks_how_we_are(text)])
                    if await self._follow_intro():
                        return
                    continue
                if not text:
                    continue
                self._heard_since_we_spoke.append(text)  # a hold message may loop from here
                item = None  # already transcribed: let the dialog look at it
                event = "The call was just answered; their first words are at the end of the conversation."
                turn_ready = True
            else:
                turn_ready = forced
            if turn_ready:
                pass  # first words already heard; go straight to the dialog
            elif item is HANGUP:
                call.set_outcome(self._hangup_outcome())
                return
            elif item is None:
                if self._superseded_event:
                    # What cut our reply short never became words: reply after all.
                    pending_event, self._superseded_event = self._superseded_event, None
                    continue
                if self._frames:
                    continue  # we're still talking
                if self.segmenter.in_speech:
                    # They're mid-sentence: a long answer only becomes an utterance when
                    # it ends, so this wasn't silence. (A 20 s answer used to get "Are
                    # you still there?" just before it finished.)
                    silent_for = 0
                    continue
                silent_for += LISTEN_SECONDS
                if silent_for >= self.hold_timeout_seconds:
                    call.set_outcome("no_response")
                    return
                if self._on_hold and not self._introduced():
                    continue  # quiet on hold: wait for a person, don't talk into the queue
                if self._farewell_said:
                    continue  # we said goodbye on an incoming call: no nudges, they hang up
                if silent_for < THINKING_SECONDS and self._we_spoke_last():
                    continue  # they're thinking (or gathering words): don't nudge yet
                if self._we_spoke_last():
                    event = f"No speech from the callee for {silent_for} seconds (on hold, or line quiet)."
                else:
                    event = (
                        f"The callee spoke last and has been quiet for {silent_for} seconds: they've "
                        "finished, it's your turn. Reply to what they said."
                    )
            else:
                silent_for = 0
                asked = self._just_asked()
                heard = [await self._heard(item)]
                # They may have kept talking while we were busy; take it all in.
                while not self.utterances.empty():
                    more = self.utterances.get_nowait()
                    if more is HANGUP:
                        call.set_outcome(self._hangup_outcome())
                        return
                    heard.append(await self._heard(more))
                # A sentence that trails off ("...after we finish the"): let them finish it.
                for _ in range(3):
                    if not sounds_unfinished(next((h for h in reversed(heard) if h), "")):
                        break
                    last = next((h for h in reversed(heard) if h), "")
                    more = await self._next_utterance(timeout=TRAILING_OFF_SECONDS if trails_off(last) else CONTINUE_SECONDS)
                    if more is None:
                        break
                    if more is HANGUP:
                        call.set_outcome(self._hangup_outcome())
                        return
                    heard.append(await self._heard(more))
                # Never start a reply while they're mid-sentence. The wait above can
                # run out just as they start again ("...in the breach of" ... "something,
                # so I need to notify him"), and then we'd talk over the rest.
                while self.segmenter.in_speech:
                    more = await self._next_utterance(timeout=MID_SENTENCE_MAX_SECONDS)
                    if more is None:
                        break
                    if more is HANGUP:
                        call.set_outcome(self._hangup_outcome())
                        return
                    heard.append(await self._heard(more))
                if not any(heard):
                    if self._superseded_event:
                        # It was only noise that cut our reply short: reply after all.
                        pending_event, self._superseded_event = self._superseded_event, None
                    continue
                said = [h for h in heard if h]
                joined = " ".join(said)
                self._farewell_said = False  # they're still talking to us
                if all(is_hold_sound(h) for h in said):
                    # Hold tones ("Beep."): nothing to answer.
                    self._on_hold = True
                    continue
                if self._on_hold and not self._introduced() and (is_promo(joined) or sounds_recorded(joined) or all(MUSIC_HALLUCINATION.fullmatch(h.strip(" .!")) for h in said)):
                    # In the queue, before anyone has heard our opening: an ad for their
                    # website or Whisper's caption for music. Stay on hold. (Once we've
                    # introduced ourselves, the model decides: it may be the person back.) A person coming back says who they are or asks
                    # how they can help, and that doesn't match.
                    log.info("call %s: recording, promotion or music while on hold, ignored", call.id)
                    self._heard_since_we_spoke.extend(said)
                    continue
                if is_repeat(joined, self._heard_since_we_spoke):
                    # The same hold message again while we stayed quiet: a recording.
                    # No reply, and no model turn spent deciding to stay silent.
                    log.info("call %s: hold message repeated, staying on hold", call.id)
                    self._on_hold = True
                    continue
                self._heard_since_we_spoke.extend(said)
                after_hold = self._on_hold and not self._introduced()
                if (self._just_pressed() or after_hold) and answered_after_menu(joined):
                    # "Harvey Norman Moore Park" right after our key press, even garbled
                    # ("Have you no more, Pak?"), or "Hi, we're with Riverside" after
                    # hold tones: a person picked up. Answer at once.
                    log.info("call %s: person answered after the menu or hold; introducing", call.id)
                    self._on_hold = False
                    await self._say(opening_lines(call.brief.opening_line)[asks_how_we_are(joined)])
                    continue
                was_on_hold = self._on_hold
                if HOLD_PHRASE.search(joined):
                    self._on_hold = True  # "Transferring you now": the dialog may thank them, no filler
                elif not RECORDING_HINT.search(joined):
                    self._on_hold = False
                if all(BARE_HELLO.match(h) for h in said) and time.monotonic() - self._last_said_at < STALE_HELLO_SECONDS:
                    # "Hello?" said over our line: it answers them, unless they didn't
                    # catch it and stay quiet afterwards; then ask again, briefly.
                    log.info("call %s: stale 'hello' already answered by our line", call.id)
                    await self._wait_drained(timeout=30)
                    more = await self._next_utterance(timeout=INTRO_PAUSE_SECONDS)
                    if more is None:
                        pending_event = (
                            "They said 'Hello?' while you were talking and have been quiet since: they "
                            "probably didn't hear you. Say it again, short: a brief hi and your question "
                            "in one sentence."
                        )
                    else:
                        self.utterances.put_nowait(more)
                    continue
                if asked and all(is_listening(h) for h in said):
                    # "Okay, go ahead" right after our question: they're listening.
                    log.info("call %s: acknowledgement after our question, waiting for the answer", call.id)
                    continue
                if all(THANKS_ONLY.match(h.strip()) for h in said) and time.monotonic() - self._last_said_at > LONE_THANKS_SECONDS:
                    # Whisper's favourite word for line noise, e.g. while they're off
                    # checking a shelf. Not worth a reply.
                    log.info("call %s: lone thank-you long after our question, ignored", call.id)
                    continue
                if asked and all(THANKS_ONLY.match(h.strip()) for h in said):
                    # Whisper hears a short answer ("three") as "Thank you." Don't drop it.
                    event = (
                        "Right after your question they said something short that speech recognition "
                        "heard as a thank-you. It may be a garbled short answer: briefly ask them to "
                        "repeat it (e.g. 'Sorry, was that three?' if you can guess)."
                    )
                elif self._heard_over_us:
                    event = (
                        "The callee spoke while you were still talking (their words are at the end of "
                        "the conversation): it may not answer your latest question. If it doesn't, "
                        "respond to what they said and wait for the answer."
                    )
                else:
                    event = "The callee just spoke (their latest words are at the end of the conversation)."
                    if was_on_hold and not PERSON_CUE.search(joined):
                        event += (
                            " You were on hold, so this may well be another recording: announcements and "
                            "promotions phrased as questions aren't for you. Stay silent unless a person is "
                            "clearly talking to you."
                        )
                    # No "Let me think." into a hold queue, or over someone who
                    # trailed off ("I think it was just the...") and may go on.
                    they_spoke = not self._on_hold and not sounds_unfinished(said[-1])
                    if all(HESITATION.match(h) for h in said):
                        # Just "Ah." and nothing after it: they may still be gathering words.
                        event = (
                            "The callee only hesitated ('ah', 'um') and went quiet. Don't move on: "
                            "briefly invite them to go on, or gently repeat your last question in a few words."
                        )

            turn = await self._turn_with_filler(event, they_spoke)
            if turn is SUPERSEDED:
                self._superseded_event = event  # in case what follows is only noise
                continue
            self._superseded_event = None
            if turn is None:
                return
            if not turn.say and not turn.dtmf and not turn.end_call and self._person_waiting():
                # A person picked up (maybe mid hold message) and the dialog chose silence.
                log.info("call %s: person on the line but no reply chosen; introducing", call.id)
                turn.say = call.brief.opening_line
            if turn.dtmf and not self._menu_playing():
                # Pressing keys at a person (who just said "hello") ends the call.
                log.info("call %s: ignoring dtmf %r, no menu in progress", call.id, turn.dtmf)
                turn.dtmf = ""
                turn.say = turn.say or call.brief.opening_line
            if turn.dtmf:
                call.add_transcript("agent", f"[pressed {turn.dtmf}]")
                await self._press(turn.dtmf)
            if turn.say and not turn.spoken:
                await self._say(turn.say)
            if turn.end_call and inbound:
                # We never hang up on a caller: Purgatory wastes their time, not ours.
                # After our goodbye we just stay quiet until they hang up.
                log.info("call %s: said goodbye; waiting for the caller to hang up", call.id)
                self._farewell_said = True
                continue
            if turn.end_call:
                talked = any(who == "callee" for who, _ in call.transcript)
                if talked and turn.outcome != "voicemail" and not says_goodbye(turn.say):
                    # Some models end the call on "No thanks, I'm just checking." A
                    # person on the other end would notice the missing goodbye.
                    log.info("call %s: ending without a goodbye; adding one", call.id)
                    await self._say("Thanks, bye!")
                if await self._still_talking_after_goodbye():
                    pending_event = (
                        "They kept talking after your goodbye (their words are at the end of the "
                        "conversation). Respond to what they said properly; wrap up again only once "
                        "they've finished."
                    )
                    continue
                call.set_outcome(turn.outcome if turn.outcome != "in_progress" else "completed")
                return

    async def _still_talking_after_goodbye(self) -> bool:
        """Don't hang up on someone mid-sentence: after our goodbye, listen a
        moment. More than a 'bye'/'thanks' means the conversation isn't over."""
        await self._wait_drained(timeout=20)
        item = await self._next_utterance(timeout=AFTER_GOODBYE_SECONDS)
        if item is None:
            return False
        if item is HANGUP:
            self.utterances.put_nowait(HANGUP)
            return False
        text = await self._heard(item)
        if not text or FAREWELL_ONLY.match(text.strip()) or is_acknowledgement(text):
            return False
        log.info("call %s: still talking after our goodbye", self.call.id)
        log.debug("call %s: after our goodbye they said %r", self.call.id, text)
        return True

    async def _press(self, digits: str) -> None:
        """Press phone-menu keys: real DTMF when we can, in-band tones otherwise
        (many menus ignore tones)."""
        if self.send_dtmf is not None:
            try:
                await self.send_dtmf(digits)
                # Menu choices are harmless, but a brief could also have it key in a reference.
                log.info("call %s: pressed %d key(s) (DTMF via AMI)", self.call.id, len(digits))
                log.debug("call %s: pressed %r", self.call.id, digits)
                return
            except Exception:
                log.exception("call %s: AMI DTMF failed, falling back to tones", self.call.id)
        self._play(dtmf.tones(digits, self.rate), interruptible=False)

    async def _turn_with_filler(self, event: str, they_spoke: bool) -> Turn | None:
        """Get the next turn; if it takes a moment after they've spoken, fill the
        pause with a short pre-rendered 'Let me think.' like a person would."""
        task = self._pending_turn = asyncio.create_task(self._turn(event))
        # Not to recorded greetings, menus or hold messages: only once we're
        # actually talking with someone.
        talking = any(who == "agent" and not text.startswith("[pressed") for who, text in self.call.transcript)
        filled = False
        if they_spoke and talking and not self._filled_last_turn:
            done, _ = await asyncio.wait({task}, timeout=FILLER_AFTER_SECONDS)
            if not done and not self._frames and not self._streamed_at:
                options = [f for f in FILLERS if f != self._last_filler]
                self._last_filler = random.choice(options)
                self._play(await self.speech.synthesize(self._last_filler))
                log.info("call %s: filler %r while thinking", self.call.id, self._last_filler)
                filled = True
        self._filled_last_turn = filled
        try:
            return await task
        except asyncio.CancelledError:
            if not self._superseded:
                raise
            log.info("call %s: they carried on before our reply started; answering all of it", self.call.id)
            return SUPERSEDED
        finally:
            self._superseded = False
            self._pending_turn = None

    async def _turn(self, event: str) -> Turn | None:
        call = self.call
        for attempt in range(2):
            self._streamed_at = 0.0
            try:
                t0 = time.monotonic()
                turn = await self.dialog.next_turn(
                    call.brief, call.transcript, call.answers, event, call.notes,
                    on_sentence=self._say_streamed,
                )
                if self._streamed_at:
                    log.info("call %s: LLM turn %.0f ms, first words after %.0f ms", call.id,
                             (time.monotonic() - t0) * 1000, (self._streamed_at - t0) * 1000)
                else:
                    log.info("call %s: LLM turn %.0f ms", call.id, (time.monotonic() - t0) * 1000)
                break
            except Exception:
                log.exception("call %s: dialog turn failed (attempt %d)", call.id, attempt + 1)
                if self._streamed_at:
                    # Part of the reply is already out: don't start over with a second one.
                    call.error = "dialog LLM failed mid-reply"
                    call.set_outcome("technical_failure")
                    return None
        else:
            call.error = "dialog LLM failed twice"
            await self._say(f"I'm sorry, I'm having technical trouble, so I'll have to go. I'll let {call.owner} know we spoke. Goodbye.")
            call.set_outcome("technical_failure")
            return None
        call.answers.update(turn.answers)
        if turn.notes and turn.notes not in call.notes:
            call.notes.append(turn.notes)
        call.changed()
        return turn

    async def _follow_intro(self) -> bool:
        """Prepare the first question while the introduction plays, and ask it if
        they stay quiet afterwards, like a person would. If they speak instead,
        drop it and let the main loop answer them. Returns True if the call
        should end."""
        call = self.call
        if call.brief.opening_line.rstrip().endswith("?"):
            return False  # the opening already asked something: wait for the answer
        pending = asyncio.create_task(
            self.dialog.next_turn(call.brief, list(call.transcript), dict(call.answers), INTRO_EVENT, list(call.notes))
        )
        await self._wait_drained(timeout=30)
        try:
            item = await asyncio.wait_for(self.utterances.get(), INTRO_PAUSE_SECONDS)
        except TimeoutError:
            item = None
        if item is not None:
            pending.cancel()
            self.utterances.put_nowait(item)  # they spoke (or hung up): the main loop handles it
            return False
        try:
            turn = await pending
        except Exception:
            log.exception("call %s: preparing the first question failed", call.id)
            return False
        call.answers.update(turn.answers)
        if turn.notes and turn.notes not in call.notes:
            call.notes.append(turn.notes)
        call.changed()
        if turn.say:
            await self._say(turn.say)
        if turn.end_call:
            call.set_outcome(turn.outcome if turn.outcome != "in_progress" else "completed")
            return True
        return False

    async def _first_words(self):
        """The first utterance after answer, or None after SILENT_ANSWER_SECONDS
        of quiet. Never gives up while they're mid-sentence."""
        waited = 0.0
        while True:
            item = await self._next_utterance(timeout=0.5)
            if item is not None:
                return item
            waited += 0.5
            if waited >= SILENT_ANSWER_SECONDS and not self.segmenter.in_speech:
                return None
            if waited >= 60:
                return None

    def _person_waiting(self) -> bool:
        """Their latest words have a person-answering cue and we haven't spoken to
        them since (only key presses, or nothing)."""
        for who, text in reversed(self.call.transcript):
            if who == "agent" and not text.startswith("[pressed"):
                return False
            if who == "callee" and PERSON_CUE.search(text):
                return True
        return False

    def _introduced(self) -> bool:
        """We've said our opening line. A hold after that is them checking
        something, and whoever comes back gets a real reply, not the intro again."""
        opening = self.call.brief.opening_line
        return any(who == "agent" and text.endswith(opening) for who, text in self.call.transcript)

    def _just_pressed(self) -> bool:
        """Our last action was a key press and we haven't spoken since."""
        for who, text in reversed(self.call.transcript):
            if who == "agent":
                return text.startswith("[pressed")
        return False

    def _we_spoke_last(self) -> bool:
        return bool(self.call.transcript) and self.call.transcript[-1][0] == "agent"

    def _just_asked(self) -> bool:
        """Our last line was a question still waiting for an answer."""
        return bool(self.call.transcript) and self.call.transcript[-1][0] == "agent" and self.call.transcript[-1][1].rstrip().endswith("?")

    def _menu_playing(self) -> bool:
        """True if the callee's latest words sound like a phone menu."""
        for who, text in reversed(self.call.transcript):
            if who == "callee":
                return bool(re.search(r"\bpress(ed)?\b|\bdial\b|\boption\b", text, re.I))
        return False

    def _hangup_outcome(self) -> str:
        """They hung up; if we already had every answer, the call still did its job."""
        brief = self.call.brief
        return "completed" if all(q in self.call.answers for q in brief.questions) else "hung_up"

    async def _heard(self, item: tuple[bytes, str | None]) -> str:
        pcm, overlapped_line = item
        text = ""
        # Roughly when they started speaking: the utterance ends about now.
        began = time.time() - len(pcm) / (2 * self.rate)
        if len(pcm) >= self.frame_bytes * MIN_UTTERANCE_MS // ass.FRAME_MS:
            t0 = time.monotonic()
            text = await self.speech.transcribe(pcm)
            # Timing at INFO; what was said only at DEBUG (LOG_LEVEL=DEBUG), so routine
            # logs don't collect names, numbers or booking references. The call
            # database has the transcript anyway.
            log.info("call %s: STT %.0f ms (%d chars)", self.call.id, (time.monotonic() - t0) * 1000, len(text))
            log.debug("call %s: heard %r", self.call.id, text)
        if text and overlapped_line and is_echo(text, overlapped_line):
            log.info("call %s: echo of our own line, ignored", self.call.id)
            log.debug("call %s: echo was %r", self.call.id, text)
            text = ""
        elif text and overlapped_line and is_acknowledgement(text):
            # "Thank you" / "okay" said over us is a reaction, not an answer.
            log.info("call %s: reaction while we spoke, not an answer", self.call.id)
            log.debug("call %s: reaction was %r", self.call.id, text)
            text = ""
        if self._paused_at is not None:
            if text:
                self._interrupt()
            else:
                log.info("call %s: noise, resuming speech", self.call.id)
                self._paused_at = None
        if text:
            self.call.add_transcript("callee", text, at=began)
            # Lets the dialog know it may not answer the question we were asking.
            self._heard_over_us = bool(overlapped_line)
        return text

    @property
    def _on_hold(self) -> bool:
        return self._on_hold_flag

    @_on_hold.setter
    def _on_hold(self, value: bool) -> None:
        # Track time on hold: it's added back to the call's time limit.
        if value and not self._on_hold_flag:
            self._hold_started = time.monotonic()
        elif not value and self._on_hold_flag and self._hold_started is not None:
            self._held_seconds += time.monotonic() - self._hold_started
            self._hold_started = None
        self._on_hold_flag = value

    def _hold_total(self) -> float:
        current = time.monotonic() - self._hold_started if self._on_hold_flag and self._hold_started else 0.0
        return self._held_seconds + current

    async def _say(self, text: str) -> None:
        """Start speaking as soon as the first sentence is rendered; the rest is
        rendered while that plays."""
        self._heard_since_we_spoke = []
        self.call.add_transcript("agent", text)
        self._speaking_line = text
        self._last_said_at = time.monotonic()
        for i, sentence in enumerate(split_sentences(text)):
            t0 = time.monotonic()
            pcm = await self.speech.synthesize(sentence)
            log.info("call %s: TTS %.0f ms (%d chars)", self.call.id, (time.monotonic() - t0) * 1000, len(sentence))
            log.debug("call %s: saying %r", self.call.id, sentence)
            if i == 0:
                self._play(pcm)
            else:
                self._frames.extend(ass.split_frames(pcm, self.rate))
                self._drained.clear()

    async def _say_streamed(self, sentence: str) -> None:
        """One sentence of the reply, as soon as it has streamed in. The first
        starts a new line in the transcript; later ones extend it."""
        first = not self._streamed_at
        if first:
            self._streamed_at = time.monotonic()  # no filler from now on
        t0 = time.monotonic()
        pcm = await self.speech.synthesize(sentence)
        log.info("call %s: TTS %.0f ms (%d chars)", self.call.id, (time.monotonic() - t0) * 1000, len(sentence))
        log.debug("call %s: saying %r", self.call.id, sentence)
        if first:
            self._heard_since_we_spoke = []
            self.call.add_transcript("agent", sentence)
            self._speaking_line = sentence
            self._last_said_at = time.monotonic()
            self._play(pcm)
            return
        who, text = self.call.transcript[-1]
        self.call.transcript[-1] = (who, f"{text} {sentence}")
        self.call.changed()
        self._speaking_line = self.call.transcript[-1][1]
        self._frames.extend(ass.split_frames(pcm, self.rate))
        self._drained.clear()

    def _play(self, pcm: bytes, interruptible: bool = True) -> None:
        # Keypad tones must play out even while a phone menu keeps talking.
        self._interruptible = interruptible
        self._paused_at = None
        self._frames.extend(ass.split_frames(pcm, self.rate))
        self._drained.clear()
        if self.barge_in:
            # Only interruptions need the stricter start; otherwise it just clips
            # the first syllables of someone answering as we finish.
            self.segmenter.start_frames = START_MS_SPEAKING // 20

    async def _next_utterance(self, timeout: float):
        try:
            return await asyncio.wait_for(self.utterances.get(), timeout)
        except TimeoutError:
            return None

    async def _wait_drained(self, timeout: float) -> None:
        try:
            await asyncio.wait_for(self._drained.wait(), timeout)
        except TimeoutError:
            pass

    # Audio in / out

    async def _read_loop(self) -> None:
        try:
            while True:
                kind, payload = await ass.read_message(self.reader)
                if kind in ass.AUDIO_RATES:
                    self._audio_clock = time.monotonic()
                    self._feed_vad(self._to_session_rate(kind, payload))
                elif kind in (ass.KIND_HANGUP, ass.KIND_ERROR):
                    break
                else:
                    log.debug("call %s: ignoring AudioSocket kind %#x", self.call.id, kind)
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            self.hung_up = True
            self.utterances.put_nowait(HANGUP)

    def _feed_vad(self, pcm: bytes) -> None:
        for event, utterance in self.segmenter.push(pcm):
            if event == "start":
                # Remember what we were saying when it started, for the echo check.
                # Answering as our question ends isn't talking over us: only count it
                # as overlap when a good part of our line was still to come.
                still_to_say = len(self._frames) * ass.FRAME_MS / 1000
                self._overlapped_line = self._speaking_line if still_to_say > OVERLAP_MIN_SECONDS else None
                self._on_speech_start()
            else:
                self.utterances.put_nowait((utterance, self._overlapped_line))

    async def _gap_filler(self) -> None:
        """Asterisk sends no frames at all while the far side is silent on some
        paths (e.g. Local channels); count missing audio as silence so the
        callee's turn still ends."""
        while True:
            await asyncio.sleep(0.1)
            missing = time.monotonic() - self._audio_clock
            if missing >= 0.1:
                frames = int(missing / 0.02)
                self._feed_vad(self.silence * frames)
                self._audio_clock += frames * 0.02

    def _to_session_rate(self, kind: int, payload: bytes) -> bytes:
        rate = ass.AUDIO_RATES[kind]
        if rate != self._in_rate:
            log.info("call %s: receiving %d Hz audio (kind %#x)", self.call.id, rate, kind)
            self._in_rate = rate
            self._resampler = soxr.ResampleStream(rate, self.rate, 1, dtype="int16") if rate != self.rate else None
        if self._resampler is None:
            return payload
        return self._resampler.resample_chunk(np.frombuffer(payload, dtype=np.int16)).tobytes()

    def _on_speech_start(self) -> None:
        if self.barge_in and self._interruptible and self._frames and self._paused_at is None:
            self._paused_at = time.monotonic()
        pending = self._pending_turn
        if pending is not None and not pending.done() and not self._streamed_at and not self._frames:
            # Still working out our reply and nothing played yet: they're carrying on.
            self._superseded = True
            pending.cancel()

    def _interrupt(self) -> None:
        """The callee really is talking over us: drop the rest of our sentence."""
        self._paused_at = None
        if self._frames:
            self._frames.clear()
            self.call.mark_interrupted()

    async def _send_loop(self) -> None:
        """Stream 20 ms frames continuously: our speech, or silence between
        turns. Asterisk drops an AudioSocket that goes quiet for 2 seconds."""
        next_at = time.monotonic()
        while True:
            if self._paused_at is not None and time.monotonic() - self._paused_at > BARGE_IN_CONFIRM_SECONDS:
                self._interrupt()  # they've kept talking: it's not noise
            if self._paused_at is not None:
                frame = self.silence
            elif self._frames:
                frame = self._frames.popleft()
            else:
                if not self._drained.is_set():
                    self.segmenter.start_frames = START_MS_IDLE // 20
                    self._drained.set()
                frame = self.silence
            self.writer.write(ass.encode(self.out_kind, frame))
            await self.writer.drain()
            next_at += 0.02
            delay = next_at - time.monotonic()
            if delay > 0:
                await asyncio.sleep(delay)
            else:
                next_at = time.monotonic()
