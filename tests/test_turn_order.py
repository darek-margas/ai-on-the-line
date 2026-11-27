"""Turn order: lines filed where they were said, and no question asked over
someone who has just started talking."""

import asyncio
import time
import uuid

from phone_agent import session as session_mod
from phone_agent.calls import CallState
from phone_agent.db import Database
from phone_agent.dialog import Brief, Turn
from phone_agent.session import CallSession

from test_call_flow import FakeDialog, FakeSpeech

BRIEF = Brief(goal="g", callee="Peter", opening_line="Hi Peter, it's Alex's assistant returning your call.",
              questions={"trees": "real trees?"})


def make_call(tmp_path) -> CallState:
    call = CallState(id=str(uuid.uuid4()), phone="0255501234", brief=BRIEF, owner="Alex",
                     db=Database(str(tmp_path)))
    call.started = time.time() - 20
    return call


def test_a_slow_transcript_goes_before_the_line_it_preceded(tmp_path):
    call = make_call(tmp_path)
    call.add_transcript("agent", "Hi Peter, it's Alex's assistant.", at=call.started + 4)
    call.add_transcript("agent", "Are you selling real trees?", at=call.started + 18)
    # "Yes?" began at 17 s, but Whisper finished it after our question had started.
    assert call.add_transcript("callee", "Yes?", at=call.started + 17) is True
    assert [w for w, _ in call.transcript] == ["agent", "callee", "agent"]
    assert call.timings == [4.0, 17.0, 18.0]
    # Said after the question: stays at the end.
    assert call.add_transcript("callee", "Yes, we are.", at=call.started + 19) is False
    assert call.transcript[-1] == ("callee", "Yes, we are.")


def test_a_callee_line_never_jumps_another_callee_line(tmp_path):
    call = make_call(tmp_path)
    call.add_transcript("callee", "Produce to Plate.", at=call.started + 10)
    call.add_transcript("callee", "Hello?", at=call.started + 5)
    assert [t for _, t in call.transcript] == ["Produce to Plate.", "Hello?"]


class PreparedQuestion(FakeDialog):
    async def next_turn(self, brief, transcript, answers, event, notes=None, on_sentence=None):
        self.calls += 1
        return Turn(say="Are you selling real trees this year?", answers={}, notes="", end_call=False,
                    outcome="in_progress")


async def test_intro_pause_lets_them_finish_before_asking(tmp_path, monkeypatch):
    monkeypatch.setattr(session_mod, "INTRO_PAUSE_SECONDS", 0.2)
    call = make_call(tmp_path)
    session = CallSession(call, None, None, FakeSpeech(), PreparedQuestion(),
                          max_call_seconds=60, hold_timeout_seconds=30, barge_in=True)
    said = []

    async def say(text, *args, **kwargs):
        said.append(text)

    session._say = say
    session.segmenter._in_speech = True  # "Yes?" has started as the pause runs out

    async def they_finish():
        await asyncio.sleep(0.6)
        session.segmenter._in_speech = False
        session.utterances.put_nowait((bytes(6400), None))

    finishing = asyncio.create_task(they_finish())
    assert await session._follow_intro() is False
    await finishing
    assert said == []  # the prepared question wasn't asked over them
    assert session.utterances.qsize() == 1  # their words go to the main loop


async def test_intro_pause_still_asks_when_they_stay_quiet(tmp_path, monkeypatch):
    monkeypatch.setattr(session_mod, "INTRO_PAUSE_SECONDS", 0.2)
    call = make_call(tmp_path)
    session = CallSession(call, None, None, FakeSpeech(), PreparedQuestion(),
                          max_call_seconds=60, hold_timeout_seconds=30, barge_in=True)
    said = []

    async def say(text, *args, **kwargs):
        said.append(text)

    session._say = say
    assert await session._follow_intro() is False
    assert said == ["Are you selling real trees this year?"]
