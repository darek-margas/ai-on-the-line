# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Darek Margas. See NOTICE.
"""Call lifecycle: queue, dial through ARI, hand the answered call to a CallSession."""

import asyncio
import logging
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from . import audiosocket as ass
from .ami import Ami
from .ari import Ari
from .config import Config
from .db import Database
from .dialog import Brief, Dialog
from .dialing import normalize_number
from .inbound import PROFILES
from .session import FILLERS, CallSession, opening_lines, split_sentences
from .speech import Speech

log = logging.getLogger(__name__)

FINAL_STATUSES = {"finished", "not_answered", "failed"}
INBOUND_CONNECT_SECONDS = 30  # registered incoming call whose audio never arrives


@dataclass
class CallState:
    id: str
    phone: str
    brief: Brief
    owner: str
    db: Database
    created: float = field(default_factory=time.time)
    status: str = "queued"
    outcome: str | None = None
    answers: dict[str, str] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    transcript: list[tuple[str, str]] = field(default_factory=list)
    # Seconds from the start of the call to each transcript line (approximate).
    timings: list[float | None] = field(default_factory=list)
    channel_id: str | None = None
    recording: str | None = None
    started: float | None = None
    ended: float | None = None
    error: str | None = None
    connected: asyncio.Event = field(default_factory=asyncio.Event)
    conversation_over: asyncio.Event = field(default_factory=asyncio.Event)
    done: asyncio.Event = field(default_factory=asyncio.Event)  # final state saved

    def add_transcript(self, who: str, text: str, at: float | None = None) -> None:
        """at: when the line was said (time.time()), if not now."""
        self.transcript.append((who, text))
        self.timings.append(round((at or time.time()) - self.started, 1) if self.started else None)
        self.changed()

    def mark_interrupted(self) -> None:
        for i in range(len(self.transcript) - 1, -1, -1):
            who, text = self.transcript[i]
            if who == "agent":
                if not text.endswith("[interrupted]"):
                    self.transcript[i] = (who, text + " [interrupted]")
                break

    def set_outcome(self, outcome: str) -> None:
        if self.outcome is None:
            self.outcome = outcome

    def changed(self) -> None:
        self.db.save_call(self.to_row())

    def to_row(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "created": self.created,
            "phone": self.phone,
            "callee": self.brief.callee,
            "brief": self.brief.to_dict(),
            "status": self.status,
            "outcome": self.outcome,
            "answers": self.answers,
            "notes": self.notes,
            "transcript": self.transcript,
            "timings": self.timings,
            "recording": self.recording,
            "started": self.started,
            "ended": self.ended,
            "error": self.error,
        }


class CallManager:
    def __init__(self, cfg: Config, db: Database, speech: Speech, dialog: Dialog, ari: Ari):
        self.cfg = cfg
        self.db = db
        self.speech = speech
        self.dialog = dialog
        self.ari = ari
        self.ami = Ami(cfg.ami_url, cfg.ami_user, cfg.ami_password) if cfg.ami_url else None
        self.calls: dict[str, CallState] = {}
        self._slots = asyncio.Semaphore(cfg.max_concurrent_calls)
        self._tasks: set[asyncio.Task] = set()

    def start_call(self, phone: str, brief: Brief) -> CallState:
        call = CallState(id=str(uuid.uuid4()), phone=phone, brief=brief, owner=self.cfg.owner_name, db=self.db)
        normalize_number(phone, self.cfg.dial_rules)  # refuse before queueing
        self.calls[call.id] = call
        call.changed()
        task = asyncio.create_task(self._run(call))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return call

    async def register_inbound(self, call_id: str, caller: str, diverted_from: str, profile: str) -> CallState:
        """The PBX is about to hand us an incoming call: set it up before its
        AudioSocket connects. Independent of outbound calls and their slots."""
        call_id = str(uuid.UUID(call_id))
        if profile not in PROFILES:
            raise ValueError(f"unknown profile {profile!r}")
        active = sum(c.brief.direction == "inbound" and c.status not in FINAL_STATUSES for c in self.calls.values())
        if active >= self.cfg.max_inbound_calls or call_id in self.calls:
            raise RuntimeError("busy")
        brief = PROFILES[profile](
            self.cfg.owner_name, caller, diverted_from, bio=self.cfg.owner_bio, pronouns=self.cfg.owner_pronouns
        )
        call = CallState(id=call_id, phone=caller or "withheld", brief=brief, owner=self.cfg.owner_name, db=self.db)
        call.status = "ringing"
        self.calls[call.id] = call
        call.changed()
        # The greeting plays the moment the audio connects: have it rendered now.
        try:
            for line in [brief.opening_line] + FILLERS:
                for sentence in split_sentences(line):
                    await self.speech.synthesize(sentence)
        except Exception as exc:
            # Give the slot back, or every later caller gets "busy" until a restart.
            call.status = "failed"
            call.error = repr(exc)
            call.ended = time.time()
            call.changed()
            call.done.set()
            raise
        task = asyncio.create_task(self._watch_inbound(call))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return call

    async def _watch_inbound(self, call: CallState) -> None:
        # Warm up while our greeting plays, so their first answer isn't slow.
        try:
            await asyncio.gather(self.dialog.warm_up(call.brief), self.speech.transcribe(bytes(self.speech.rate)))
        except Exception:
            log.warning("call %s: warm-up failed", call.id, exc_info=True)
        try:
            await asyncio.wait_for(call.connected.wait(), INBOUND_CONNECT_SECONDS)
        except TimeoutError:
            call.status = "failed"
            call.error = "the PBX never connected the audio"
            call.ended = time.time()
            call.changed()
            call.done.set()

    def get(self, call_id: str) -> dict[str, Any] | None:
        if call_id in self.calls:
            return self.calls[call_id].to_row()
        return self.db.get_call(call_id)

    async def wait(self, call_id: str, seconds: float) -> None:
        call = self.calls.get(call_id)
        if call is None or call.done.is_set():
            return
        try:
            await asyncio.wait_for(call.done.wait(), seconds)
        except TimeoutError:
            pass

    async def hang_up(self, call_id: str) -> bool:
        call = self.calls.get(call_id)
        if call is None or call.status in FINAL_STATUSES:
            return False
        call.set_outcome("hung_up_by_owner")
        if call.channel_id:
            await self.ari.hangup(call.channel_id)
        # A call still queued or warming up is stopped by _run before it dials.
        return True

    async def _run(self, call: CallState) -> None:
        try:
            async with self._slots:
                # Wake the LLM and Whisper and pre-render both openings before
                # dialing, so the callee never waits on a cold start.
                try:
                    await self.dialog.warm_up(call.brief)
                    await self.speech.transcribe(bytes(self.speech.rate))  # 0.5 s of silence
                except Exception:
                    log.warning("call %s: warm-up failed, calling anyway", call.id, exc_info=True)
                for line in opening_lines(call.brief.opening_line) + FILLERS:
                    for sentence in split_sentences(line):  # as _say will ask for them
                        await self.speech.synthesize(sentence)
                if call.outcome == "hung_up_by_owner":
                    return  # cancelled while queued or warming up: never dial
                call.status = "dialing"
                call.changed()
                number = normalize_number(call.phone, self.cfg.dial_rules)
                call.channel_id = await self.ari.originate(
                    self.cfg.dial_endpoint.format(number=number),
                    call.id,
                    self.cfg.caller_id,
                    self.cfg.audiosocket_advertise,
                    self.cfg.ring_timeout_seconds,
                )
                if call.outcome == "hung_up_by_owner":
                    await self.ari.hangup(call.channel_id)  # cancelled while the dial request was in flight
                    return
                if await self._wait_connected(call):
                    await call.conversation_over.wait()
                else:
                    call.status = "not_answered"
                    call.set_outcome("not_answered")
                    await self.ari.hangup(call.channel_id)
        except Exception as exc:
            log.exception("call %s failed", call.id)
            call.status = "failed"
            call.error = call.error or repr(exc)
        finally:
            if call.status not in FINAL_STATUSES:
                call.status = "finished"
            call.ended = time.time()
            call.changed()
            call.done.set()

    async def _wait_connected(self, call: CallState) -> bool:
        """Wait for AudioSocket to connect, or for the channel to vanish (busy / no answer)."""
        deadline = time.monotonic() + self.cfg.ring_timeout_seconds + 15
        while time.monotonic() < deadline:
            try:
                await asyncio.wait_for(call.connected.wait(), 3)
                return True
            except TimeoutError:
                if not await self.ari.channel_exists(call.channel_id):
                    return call.connected.is_set()
        return False

    async def handle_audiosocket(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            call_id = await ass.read_call_uuid(reader)
        except (ValueError, asyncio.IncompleteReadError) as exc:
            log.warning("bad AudioSocket connection: %s", exc)
            writer.close()
            return
        call = self.calls.get(call_id)
        if call is None or call.connected.is_set():
            log.warning("AudioSocket for unknown call %s", call_id)
            writer.write(ass.encode(ass.KIND_HANGUP))
            writer.close()
            return

        call.status = "connected"
        call.started = time.time()
        call.recording = f"phone-agent/{call.id}.wav"
        call.connected.set()
        call.changed()

        inbound = call.brief.direction == "inbound"
        send_dtmf = None
        if self.ami is not None and not inbound:
            async def send_dtmf(digits: str) -> None:
                # Real keypad events, sent on as RFC 2833 by the trunk.
                await self.ami.play_dtmf(await self.ari.channel_name(call.channel_id), digits)

        session = CallSession(
            call,
            reader,
            writer,
            self.speech,
            self.dialog,
            max_call_seconds=self.cfg.inbound_max_seconds if inbound else self.cfg.max_call_seconds,
            hold_timeout_seconds=self.cfg.hold_timeout_seconds,
            barge_in=self.cfg.barge_in,
            send_dtmf=send_dtmf,
        )
        try:
            await session.run()
        finally:
            call.status = "failed" if call.error and not call.transcript else "finished"
            call.conversation_over.set()
            if inbound:  # outbound calls are wrapped up by _run
                call.ended = time.time()
                call.changed()
                call.done.set()
