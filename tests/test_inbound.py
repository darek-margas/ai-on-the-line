# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Darek Margas. See NOTICE.
"""Incoming calls: the PBX registers the call, then connects its AudioSocket."""

import asyncio
import uuid

import pytest

from phone_agent import audiosocket as ass
from phone_agent.calls import CallManager
from phone_agent.db import Database
from phone_agent.dialog import SYSTEM_PROMPT, Dialog
from phone_agent.inbound import screener_brief

from test_call_flow import SILENCE, SPEECH, FakeDialog, FakeSpeech, make_config


async def fake_pbx(port: int, call_id: str, received: list[int]):
    """Asterisk handing over an answered call: a short "hello" after our greeting,
    then the caller hangs up (the screener never does)."""
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(ass.encode(ass.KIND_UUID, uuid.UUID(call_id).bytes))

    async def listen():
        while True:
            kind, _ = await ass.read_message(reader)
            received.append(kind)
            if kind == ass.KIND_HANGUP:
                return

    listener = asyncio.create_task(listen())
    for frame in [SILENCE] * 50 + [SPEECH] * 25 + [SILENCE] * 300:
        if listener.done():
            break
        writer.write(ass.encode(ass.KIND_AUDIO, frame))
        await writer.drain()
        await asyncio.sleep(0.02)
    if not listener.done():
        writer.write(ass.encode(ass.KIND_HANGUP))  # the caller hangs up
        await writer.drain()
        await asyncio.sleep(0.5)
        listener.cancel()
    writer.close()


@pytest.mark.asyncio
async def test_inbound_call_greets_first_then_converses(tmp_path):
    cfg = make_config(tmp_path)
    db = Database(cfg.data_dir)
    dialog, inbound_dialog = FakeDialog(), FakeDialog()  # INBOUND_DIALOG_LLM set
    manager = CallManager(cfg, db, FakeSpeech(), dialog, None, inbound_dialog)
    server = await asyncio.start_server(manager.handle_audiosocket, "127.0.0.1", 0)

    import phone_agent.vad as vad
    orig = vad.webrtcvad.Vad.is_speech
    vad.webrtcvad.Vad.is_speech = lambda self, frame, rate: max(frame) > 8
    received: list[int] = []
    try:
        call_id = str(uuid.uuid4())
        call = await manager.register_inbound(call_id, "0491570156", "491570159", "screener")
        with pytest.raises(RuntimeError):  # one incoming call at a time by default
            await manager.register_inbound(str(uuid.uuid4()), "0491570110", "", "screener")
        pbx = asyncio.create_task(fake_pbx(server.sockets[0].getsockname()[1], call_id, received))
        await asyncio.wait_for(call.done.wait(), 30)
        await asyncio.wait_for(pbx, 10)
    finally:
        vad.webrtcvad.Vad.is_speech = orig
        server.close()

    saved = db.get_call(call_id)
    assert saved["status"] == "finished"
    assert saved["brief"]["direction"] == "inbound"
    assert "forwarded from Alex's mobile" in saved["callee"]
    # We picked up, so we speak first; then their words, then the dialog's reply.
    assert [who for who, _ in saved["transcript"]] == ["agent", "callee", "agent"]
    assert saved["transcript"][0][1] == call.brief.opening_line
    # It said goodbye but left hanging up to the caller.
    assert ass.KIND_HANGUP not in received
    # The incoming call talked through its own model, not the outgoing one.
    assert inbound_dialog.calls and not dialog.calls


@pytest.mark.asyncio
async def test_inbound_refuses_bad_requests(tmp_path):
    manager = CallManager(make_config(tmp_path), Database(str(tmp_path)), FakeSpeech(), FakeDialog(), None)
    with pytest.raises(ValueError):
        await manager.register_inbound("not-a-uuid", "0491570156", "", "screener")
    with pytest.raises(ValueError):
        await manager.register_inbound(str(uuid.uuid4()), "0491570156", "", "no-such-profile")


def test_screener_uses_the_inbound_prompt():
    brief = screener_brief("Alex", "")
    assert brief.direction == "inbound" and "withheld" in brief.callee
    # No bio, no pronouns set: nothing personal to share, and "their assistant".
    assert not any(s.startswith("about Alex") for s in brief.may_share)
    assert "their assistant" in brief.opening_line and "About Alex" not in brief.context
    assert "this call is recorded" in brief.opening_line and brief.opening_line.endswith("What's it about?")
    assert "recorded" not in screener_brief("Alex", "", recording_notice=False).opening_line
    with_bio = screener_brief("Alex", "", bio="Alex restores old motorbikes.", pronouns="She")
    assert "about Alex: Alex restores old motorbikes." in with_bio.may_share
    assert "her assistant" in with_bio.opening_line and "About Alex: Alex restores old motorbikes." in with_bio.context
    dialog = Dialog.__new__(Dialog)
    dialog.owner, dialog.provider, dialog.agent_name = "Alex", "ollama", "Jenny"
    captured = {}

    async def fake_ask(system, user, schema):
        captured.update(system=system, user=user)
        return '{"say": "Hi", "dtmf": "", "answers": [], "notes": "", "end_call": false, "outcome": "in_progress"}'

    dialog._ask_ollama = fake_ask
    asyncio.run(dialog.next_turn(brief, [("callee", "Hello")], {}, "event"))
    assert captured["system"] != SYSTEM_PROMPT.format(owner="Alex", must_not="", recording="", name_rule="")
    assert "answering a live phone call" in captured["system"]
    # Asked its name, it says Jenny; the introduction doesn't change.
    assert "Your name is Jenny. When someone asks your name" in captured["system"]
    assert "Jenny" not in brief.opening_line
    dialog.agent_name = ""
    asyncio.run(dialog.next_turn(brief, [("callee", "Hello")], {}, "event"))
    assert "Jenny" not in captured["system"] and "say you're Alex's assistant" in captured["system"]
    assert "Caller: Hello" in captured["user"]


@pytest.mark.asyncio
async def test_failed_greeting_frees_the_inbound_slot(tmp_path):
    """If Piper fails while the greeting is prepared, the next caller mustn't get 'busy'."""

    class BrokenSpeech(FakeSpeech):
        async def synthesize(self, text):
            raise ConnectionError("piper down")

    cfg = make_config(tmp_path)
    manager = CallManager(cfg, Database(cfg.data_dir), BrokenSpeech(), FakeDialog(), None)
    with pytest.raises(ConnectionError):
        await manager.register_inbound(str(uuid.uuid4()), "0491570156", "", "screener")
    manager.speech = FakeSpeech()  # Piper is back
    call = await manager.register_inbound(str(uuid.uuid4()), "0491570157", "", "screener")
    assert call.status == "ringing"


class StuckDialog(FakeDialog):
    """An LLM (or speech server) that never answers."""

    async def next_turn(self, *args, **kwargs):
        self.calls += 1
        await asyncio.Event().wait()


async def _inbound_with(tmp_path, dialog, cfg=None, act=None):
    cfg = cfg or make_config(tmp_path)
    db = Database(cfg.data_dir)
    manager = CallManager(cfg, db, FakeSpeech(), dialog, None)
    server = await asyncio.start_server(manager.handle_audiosocket, "127.0.0.1", 0)
    import phone_agent.vad as vad
    orig = vad.webrtcvad.Vad.is_speech
    vad.webrtcvad.Vad.is_speech = lambda self, frame, rate: max(frame) > 8
    received: list[int] = []
    try:
        call_id = str(uuid.uuid4())
        call = await manager.register_inbound(call_id, "0491570156", "", "screener")
        pbx = asyncio.create_task(fake_pbx(server.sockets[0].getsockname()[1], call_id, received))
        if act:
            await act(manager, call)
        await asyncio.wait_for(call.done.wait(), 15)
        await asyncio.wait_for(pbx, 10)
    finally:
        vad.webrtcvad.Vad.is_speech = orig
        server.close()
    return call, received


@pytest.mark.asyncio
async def test_owner_hangs_up_an_incoming_call(tmp_path):
    """An incoming call has no channel of ours to hang up: the session does it."""
    dialog = StuckDialog()

    async def hang_up_mid_turn(manager, call):
        while not dialog.calls:  # the caller has spoken and we're thinking
            await asyncio.sleep(0.05)
        assert await manager.hang_up(call.id)

    call, received = await _inbound_with(tmp_path, dialog, act=hang_up_mid_turn)
    assert call.outcome == "hung_up_by_owner"
    assert ass.KIND_HANGUP in received


@pytest.mark.asyncio
async def test_time_limit_holds_while_a_turn_is_stuck(tmp_path, monkeypatch):
    import dataclasses

    import phone_agent.session as session

    monkeypatch.setattr(session, "DEADLINE_GRACE_SECONDS", 0)
    cfg = dataclasses.replace(make_config(tmp_path), inbound_max_seconds=2)
    call, received = await _inbound_with(tmp_path, StuckDialog(), cfg)
    assert call.outcome == "time_limit"
    assert ass.KIND_HANGUP in received
