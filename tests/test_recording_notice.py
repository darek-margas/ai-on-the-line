# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Darek Margas. See NOTICE.
"""Their recording tells us the call is recorded; we tell their recording ours is too."""

import asyncio

from phone_agent.dialog import Brief, Dialog
from phone_agent.session import (
    HEARD_RECORDING_NOTICE, RECORDING_NOTICE, CallSession, answered_after_menu, hold_noise,
)

from test_call_flow import FakeDialog, FakeSpeech


def test_recording_announcements_are_recognised():
    for said in ("Thanks for calling Telstra. This call is being recorded.",
                 "We are recording this call and using your phone number to address your inquiry.",
                 "Calls may be monitored or recorded for quality and training purposes.",
                 "Your call will be recorded."):
        assert HEARD_RECORDING_NOTICE.search(said), said
    for said in ("Thanks for calling Telstra.", "I've recorded your details.", "Please hold."):
        assert not HEARD_RECORDING_NOTICE.search(said), said


def test_hold_music_captions_are_not_a_person():
    # Telstra's queue, 2026-11-18: this was taken for a person answering.
    for noise in ("Music Music Music Music Thank you.", "The The The The The", "Thank you.", "Music"):
        assert hold_noise(noise) and not answered_after_menu(noise), noise
    for person in ("Harvey Norman Moore Park", "Hi, this is Ness."):
        assert not hold_noise(person) and answered_after_menu(person), person


class Call:
    def __init__(self, brief):
        self.id, self.brief, self.transcript = "x", brief, []

    def add_transcript(self, who, text):
        self.transcript.append((who, text))


def session_for(brief):
    session = CallSession(Call(brief), None, None, FakeSpeech(), FakeDialog(), max_call_seconds=60,
                          hold_timeout_seconds=30, barge_in=False)
    said = []

    async def say(text):
        said.append(text)
        session.call.add_transcript("agent", text)

    session._say = say
    return session, said


def brief(**kw):
    return Brief(goal="g", callee="c", opening_line="Hi, I'm Alex's assistant.", questions={"q": "q?"}, **kw)


def test_notice_goes_to_the_machine_once():
    session, said = session_for(brief(recording_notice=True))
    asyncio.run(session._recording_notice("Thanks for calling Telstra. This call is being recorded."))
    asyncio.run(session._recording_notice("Calls may be recorded for training purposes."))
    assert said == [RECORDING_NOTICE]


def test_no_notice_to_a_person_or_when_off():
    session, said = session_for(brief(recording_notice=True))
    asyncio.run(session._recording_notice("Hi, this is Ness speaking, just so you know this call is recorded."))
    assert said == []
    session.call.add_transcript("agent", "Hi, I'm Alex's assistant.")  # introduced: a person is on
    asyncio.run(session._recording_notice("This call is being recorded."))
    assert said == []
    session, said = session_for(brief())  # switched off for this call
    asyncio.run(session._recording_notice("This call is being recorded."))
    assert said == []


def test_model_knows_only_when_switched_on():
    dialog = Dialog.__new__(Dialog)
    dialog.owner, dialog.provider = "Alex", "ollama"
    systems = []

    async def fake_ask(system, user, schema):
        systems.append(system)
        return '{"say": "", "dtmf": "", "answers": [], "notes": "", "end_call": false, "outcome": "in_progress"}'

    dialog._ask_ollama = fake_ask
    for flag in (True, False):
        asyncio.run(dialog.next_turn(brief(recording_notice=flag), [("callee", "Hello")], {}, "event"))
    assert "records this call" in systems[0] and "records this call" not in systems[1]


def test_a_call_can_ask_for_a_longer_limit(tmp_path):
    from phone_agent.calls import CallManager
    from phone_agent.db import Database

    from test_call_flow import make_config

    cfg = make_config(tmp_path)

    async def place():
        manager = CallManager(cfg, Database(cfg.data_dir), FakeSpeech(), FakeDialog(), None)
        call = manager.start_call("7999", brief(), max_seconds=1800)
        await manager.hang_up(call.id)
        return call

    assert asyncio.run(place()).schedule["max_seconds"] == 1800


def test_a_person_off_the_queue_is_greeted_at_once():
    from phone_agent.session import person_after_hold

    # Telstra, 2026-11-18: these went to the model as "maybe a recording".
    for person in ("good morning welcome to Telstra this is Sherry from the sales team based in  Adelaide how may I help you today",
                   "Hello, welcome to Teltrack Consumer Sales Team Melbourne.  This is Ness. How can I help you today?"):
        assert person_after_hold(person), person
    for machine in ("In a sentence, please tell me how we can help you today.",
                    "Hi, this is Telstra's virtual assistant. How can I help?",
                    "Shop the latest device, gaming and tech accessories in the MyTelstra app.",
                    "Our teams are helping other customers at the moment. The estimated wait time is 9 minutes."):
        assert not person_after_hold(machine), machine
