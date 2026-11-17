# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Darek Margas. See NOTICE.
"""Call lifecycle: queue, dial through ARI, hand the answered call to a CallSession."""

import asyncio
import logging
import re
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from . import audiosocket as ass
from .ami import Ami
from .ari import Ari
from .config import Config
from .db import Database
from .dialog import Brief, Dialog
from .dialing import normalize_number
from .inbound import PROFILES
from .session import FILLERS, RECORDING_NOTICE, CallSession, opening_lines, split_sentences
from .speech import Speech

log = logging.getLogger(__name__)

FINAL_STATUSES = {"finished", "not_answered", "failed", "cancelled"}
PENDING_STATUSES = {"awaiting_approval", "scheduled"}  # not dialling yet, hold no slot
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
    # run_at, zone, ignore_hours (calling-hours check), approval: "not_needed" |
    # "needed" | "approved" | "rejected", approval_note, approved_at.
    schedule: dict[str, Any] = field(default_factory=dict)
    wake: asyncio.Event = field(default_factory=asyncio.Event)  # approved, rejected or cancelled
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
            "schedule": self.schedule,
        }


class CallManager:
    def __init__(self, cfg: Config, db: Database, speech: Speech, dialog: Dialog, ari: Ari,
                 inbound_dialog: Dialog | None = None):
        self.cfg = cfg
        self.db = db
        self.speech = speech
        self.dialog = dialog
        self.inbound_dialog = inbound_dialog or dialog  # INBOUND_DIALOG_LLM, if set
        self.ari = ari
        self.ami = Ami(cfg.ami_url, cfg.ami_user, cfg.ami_password) if cfg.ami_url else None
        self.calls: dict[str, CallState] = {}
        self._slots = asyncio.Semaphore(cfg.max_concurrent_calls)
        self._tasks: set[asyncio.Task] = set()

    def start_call(self, phone: str, brief: Brief, run_at: float | None = None, zone: str | None = None,
                   ignore_hours: bool = False, approved_by: str | None = None,
                   max_seconds: int | None = None) -> CallState:
        """Queue an outgoing call. It may first wait for the owner's approval
        (CALL_APPROVAL) and for run_at. approved_by: who already approved it.
        max_seconds: this call's time limit, if not MAX_CALL_SECONDS."""
        number = normalize_number(phone, self.cfg.dial_rules)  # refuse before queueing
        why = approved_by or self.auto_approval(number)
        call = CallState(id=str(uuid.uuid4()), phone=phone, brief=brief, owner=self.cfg.owner_name, db=self.db)
        call.schedule = {
            "run_at": run_at,
            "zone": zone or self.cfg.timezone,
            "ignore_hours": ignore_hours,
            "approval": "not_needed" if why else "needed",
            "approval_note": why or "",
            "max_seconds": max_seconds,
        }
        call.status = "awaiting_approval" if not why else "scheduled" if run_at and run_at > time.time() else "queued"
        self.calls[call.id] = call
        call.changed()
        self._spawn(call)
        return call

    def _spawn(self, call: CallState) -> None:
        task = asyncio.create_task(self._run(call))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def auto_approval(self, number: str) -> str | None:
        """Why a call to this number needs no approval, or None if it does."""
        mode = self.cfg.call_approval
        if mode == "off":
            return "approval is off"
        if mode == "all":
            return None
        rules = self.cfg.dial_rules
        if rules.extensions and re.fullmatch(rules.extensions, number):
            return "internal extension"
        listed = set()
        for entry in self.cfg.approved_numbers:
            try:
                listed.add(normalize_number(entry, rules))
            except ValueError:
                pass
        if number in listed:
            return "in APPROVED_NUMBERS"
        if self.db.number_approved(number):
            return "number approved before"
        return None

    def approve(self, call_id: str, remember: bool = False) -> bool:
        call = self.calls.get(call_id)
        if call is None or call.status != "awaiting_approval":
            return False
        call.schedule.update(approval="approved", approval_note="approved by the owner", approved_at=time.time())
        run_at = call.schedule.get("run_at")
        call.status = "scheduled" if run_at and run_at > time.time() else "queued"
        if remember and self.cfg.call_approval == "new_numbers":
            self.db.approve_number(normalize_number(call.phone, self.cfg.dial_rules), call.id)
        call.changed()
        call.wake.set()
        return True

    def reject(self, call_id: str) -> bool:
        call = self.calls.get(call_id)
        if call is None or call.status != "awaiting_approval":
            return False
        call.schedule["approval"] = "rejected"
        call.set_outcome("rejected")
        call.status = "cancelled"
        call.changed()
        call.wake.set()
        return True

    def resume_pending(self) -> int:
        """At start: calls that were waiting for approval or their time wait on."""
        rows = self.db.pending_calls()
        for row in rows:
            call = CallState(id=row["id"], phone=row["phone"], brief=Brief(**row["brief"]), owner=self.cfg.owner_name,
                             db=self.db, created=row["created"], status=row["status"], schedule=row["schedule"] or {})
            self.calls[call.id] = call
            self._spawn(call)
        return len(rows)

    def hours_error(self, phone: str, zone: str, ignore: bool, at: float | None = None) -> str | None:
        """Why the call can't go out at `at` (default now) under CALL_HOURS, or None."""
        if ignore or len(re.sub(r"\D", "", phone)) <= 5:  # extensions ring any time
            return None
        tz = ZoneInfo(zone)
        local = datetime.fromtimestamp(at or time.time(), tz)
        start, end = self.cfg.call_hours
        if start <= local.strftime("%H:%M") < end:
            return None
        return f"It's {local:%H:%M} {tz.key} for the callee then; calling hours are {start}-{end}."

    async def _go_ahead(self, call: CallState) -> bool:
        """Wait for approval and for run_at. False: rejected, cancelled or expired."""
        while True:
            call.wake.clear()
            if call.status == "cancelled":
                return False
            if call.outcome == "hung_up_by_owner":
                call.status = "cancelled"
                return False
            now = time.time()
            run_at = call.schedule.get("run_at")
            if call.schedule.get("approval") == "needed":
                deadline = (run_at or call.created) + self.cfg.approval_hours * 3600
                if now >= deadline:
                    call.status = "cancelled"
                    call.set_outcome("expired")
                    call.error = "Nobody approved it in time."
                    return False
                wait = deadline - now
            elif run_at and run_at > now:
                wait = run_at - now
            else:
                return True
            try:
                await asyncio.wait_for(call.wake.wait(), min(wait, 3600))
            except TimeoutError:
                pass

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
            self.cfg.owner_name, caller, diverted_from, bio=self.cfg.owner_bio, pronouns=self.cfg.owner_pronouns,
            recording_notice=self.cfg.screener_recording_notice,
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
            await asyncio.gather(self.inbound_dialog.warm_up(call.brief), self.speech.transcribe(bytes(self.speech.rate)))
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
        call.wake.set()  # stops a call that's waiting for approval or its time
        if call.channel_id:
            await self.ari.hangup(call.channel_id)
        # A call still queued or warming up is stopped by _run before it dials.
        return True

    async def _run(self, call: CallState) -> None:
        try:
            if not await self._go_ahead(call):
                return
            if call.status != "queued":
                call.status = "queued"
                call.changed()
            if self._too_late(call):
                return
            async with self._slots:
                # Wake the LLM and Whisper and pre-render both openings before
                # dialing, so the callee never waits on a cold start.
                try:
                    await self.dialog.warm_up(call.brief)
                    await self.speech.transcribe(bytes(self.speech.rate))  # 0.5 s of silence
                except Exception:
                    log.warning("call %s: warm-up failed, calling anyway", call.id, exc_info=True)
                notice = [RECORDING_NOTICE] if call.brief.recording_notice else []
                for line in opening_lines(call.brief.opening_line) + FILLERS + notice:
                    for sentence in split_sentences(line):  # as _say will ask for them
                        await self.speech.synthesize(sentence)
                if call.outcome == "hung_up_by_owner":
                    return  # cancelled while queued or warming up: never dial
                # Again: it may have waited a long time for a slot.
                if self._too_late(call):
                    return
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

    def _too_late(self, call: CallState) -> bool:
        """Outside calling hours: the call fails instead of dialling."""
        s = call.schedule
        late = self.hours_error(call.phone, s.get("zone") or self.cfg.timezone, bool(s.get("ignore_hours")))
        if late:
            call.status = "failed"
            call.error = f"Not dialled: {late}"
        return bool(late)

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
            self.inbound_dialog if inbound else self.dialog,
            max_call_seconds=(self.cfg.inbound_max_seconds if inbound
                              else call.schedule.get("max_seconds") or self.cfg.max_call_seconds),
            hold_timeout_seconds=self.cfg.hold_timeout_seconds,
            max_hold_seconds=self.cfg.max_hold_seconds,
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
