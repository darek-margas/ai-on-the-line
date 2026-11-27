"""Fixes from the code audit: interruptions in a live call, a speech server
failing mid-call, and the dialling rules."""

import asyncio
import dataclasses
import time
import uuid
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from phone_agent import audiosocket as ass
from phone_agent.calls import CallManager, CallState
from phone_agent.config import _call_hours
from phone_agent.db import Database
from phone_agent.dialing import DialRules, is_extension, normalize_number
from phone_agent.dialog import Brief
from phone_agent.session import CallSession, unavailable_line

from test_call_flow import FakeAri, FakeDialog, FakeSpeech, make_config
from test_inbound import fake_pbx

OPENING = "Hi, it's Alex's assistant calling about a room."
BRIEF = Brief(goal="g", callee="Hotel", opening_line=OPENING, questions={"price": "price?"})


def make_session(tmp_path, speech=None, dialog=None) -> CallSession:
    call = CallState(id=str(uuid.uuid4()), phone="0255501234", brief=BRIEF, owner="Alex",
                     db=Database(str(tmp_path)))
    call.started = time.time()
    return CallSession(call, None, None, speech or FakeSpeech(), dialog or FakeDialog(),
                       max_call_seconds=60, hold_timeout_seconds=30, barge_in=True)


# Live call


async def test_interrupted_opening_still_counts_as_introduced(tmp_path):
    session = make_session(tmp_path)
    await session._say(OPENING)
    assert session._introduced()
    session._interrupt()  # they spoke over it
    assert session.call.transcript[-1][1].endswith("[interrupted]")
    assert session._introduced()


async def test_sentences_still_rendering_are_dropped_after_an_interruption(tmp_path):
    session = make_session(tmp_path)

    class Speech(FakeSpeech):
        calls = 0

        async def synthesize(self, text):
            Speech.calls += 1
            if Speech.calls == 2:
                session._interrupt()  # they cut in while sentence two renders
            return await super().synthesize(text)

    session.speech = Speech()
    await session._say("First sentence. Second sentence. Third sentence.")
    assert not session._frames  # nothing queued to play over them
    assert Speech.calls == 2  # the third was never rendered
    assert session.call.transcript[-1][1].endswith("[interrupted]")


async def test_streamed_sentences_after_an_interruption_stay_unsaid(tmp_path):
    session = make_session(tmp_path)
    await session._say_streamed("Sure, I can help with that.")
    session._interrupt()
    await session._say_streamed("Here is a long explanation.")
    assert not session._frames
    assert session.call.transcript == [("agent", "Sure, I can help with that. [interrupted]")]
    # The next reply starts clean.
    session._streamed_at = 0.0
    await session._say_streamed("Sorry, go on.")
    await session._say_streamed("I'm listening.")
    assert session.call.transcript[-1] == ("agent", "Sorry, go on. I'm listening.")


async def test_talking_over_the_filler_supersedes_the_reply(tmp_path):
    session = make_session(tmp_path)
    session._pending_turn = pending = asyncio.ensure_future(asyncio.sleep(30))
    session._play(bytes(640) * 50, filler=True)
    session._on_speech_start()
    await asyncio.sleep(0)
    assert pending.cancelled() and session._superseded
    assert not session._frames and session._paused_at is None  # the filler is dropped, nothing resumes


async def test_talking_over_a_real_line_only_pauses_it(tmp_path):
    session = make_session(tmp_path)
    session._pending_turn = pending = asyncio.ensure_future(asyncio.sleep(30))
    session._play(bytes(640) * 50)
    session._on_speech_start()
    await asyncio.sleep(0)
    assert not pending.cancelled() and session._frames and session._paused_at is not None
    pending.cancel()


class DeafSpeech(FakeSpeech):
    """Whisper is down; Piper works."""

    async def transcribe(self, pcm):
        raise ConnectionError("Whisper is gone")


async def test_whisper_failing_mid_call_gets_an_apology_not_a_dropped_line(tmp_path, monkeypatch):
    import phone_agent.vad as vad

    monkeypatch.setattr(vad.webrtcvad.Vad, "is_speech", lambda self, frame, rate: max(frame) > 8)
    cfg = make_config(tmp_path)
    manager = CallManager(cfg, Database(cfg.data_dir), DeafSpeech(), FakeDialog(), None)
    server = await asyncio.start_server(manager.handle_audiosocket, "127.0.0.1", 0)
    received: list[int] = []
    try:
        call_id = str(uuid.uuid4())
        call = await manager.register_inbound(call_id, "0491570156", "", "screener")
        pbx = asyncio.create_task(fake_pbx(server.sockets[0].getsockname()[1], call_id, received))
        await asyncio.wait_for(call.done.wait(), 20)
        await asyncio.wait_for(pbx, 10)
    finally:
        server.close()
    assert call.outcome == "technical_failure"
    assert call.transcript[-1] == ("agent", unavailable_line(cfg.owner_name, True))
    assert ass.KIND_HANGUP in received


class BlipSpeech(FakeSpeech):
    """The first sound after answer is a click: Whisper returns nothing."""

    async def transcribe(self, pcm):
        return ""


async def test_a_click_after_answer_still_gets_the_introduction(tmp_path, monkeypatch):
    import phone_agent.vad as vad

    monkeypatch.setattr(vad.webrtcvad.Vad, "is_speech", lambda self, frame, rate: max(frame) > 8)
    cfg = make_config(tmp_path)
    manager = CallManager(cfg, Database(cfg.data_dir), BlipSpeech(), FakeDialog(), None)
    server = await asyncio.start_server(manager.handle_audiosocket, "127.0.0.1", 0)
    manager.ari = FakeAri(server.sockets[0].getsockname()[1])
    try:
        call = manager.start_call("0255501234", BRIEF)
        await asyncio.wait_for(call.done.wait(), 30)
    finally:
        server.close()
    assert call.transcript[0] == ("agent", OPENING)


# Dialling rules


def refused(phone, rules):
    with pytest.raises(ValueError):
        normalize_number(phone, rules)
    return True


def test_block_pattern_holds_however_the_number_is_written():
    rules = DialRules(block_pattern="0491570156")
    assert refused("0491570156", rules) and refused("+61 491 570 156", rules) and refused("61491570156", rules)
    assert refused("0491 570 156", DialRules(block_pattern="61491570156"))  # blocked in the other form
    assert normalize_number("0491570157", rules) == "0491570157"


def test_sharing_a_country_code_is_not_being_in_the_country():
    us = DialRules(country="US")
    assert refused("876 555 1234", us) and refused("+1 876 555 1234", us)  # Jamaica
    assert normalize_number("202 456 1111", us) == "2024561111"
    assert normalize_number("876 555 1234", dataclasses.replace(us, allow_international=True))


def test_every_countrys_emergency_numbers_are_refused_plain_and_behind_a_prefix():
    """000 in Australia, 112/997/998/999 in Poland, 15/17/18 in France, 110/119 in
    Japan...: whatever libphonenumber lists for the country, also behind the 0
    or 9 a PBX may take for an outside line."""
    import re

    from phonenumbers.phonemetadata import PhoneMetadata
    from phonenumbers.shortdata import _AVAILABLE_REGION_CODES

    checked, dialled = 0, []
    for region in sorted(_AVAILABLE_REGION_CODES):
        metadata = PhoneMetadata.short_metadata_for_region(region)
        pattern = metadata.emergency.national_number_pattern if metadata and metadata.emergency else None
        if not pattern:
            continue
        rules = DialRules(country=region)
        for length in (2, 3, 4):
            for n in range(10 ** length):
                number = str(n).zfill(length)
                if re.fullmatch(pattern, number):
                    checked += 1
                    for form in (number, "0" + number, "9" + number):
                        try:
                            normalize_number(form, rules)
                            dialled.append((region, form))
                        except ValueError:
                            pass
    assert checked > 500 and dialled == []


def test_emergency_rules_leave_real_extensions_alone():
    au, pl = DialRules(), DialRules(country="PL")
    for extension in ["101", "104", "200", "600", "7999", "1000"]:
        assert normalize_number(extension, au) == extension
    assert refused("000", au) and refused("0000", au) and refused("9000", au) and refused("0112", au)
    assert refused("997", pl) and refused("998", pl) and refused("999", pl) and refused("9997", pl)
    assert normalize_number("101", pl) == "101"
    # A PBX that really has extension 9000 can say so.
    assert normalize_number("9000", dataclasses.replace(au, allow_pattern="9000")) == "9000"
    # More digits that still connect, where the country's network allows it (911 + anything in the US).
    assert refused("9111", DialRules(country="US"))


def test_public_short_numbers_are_not_extensions(tmp_path):
    au, us = DialRules(), DialRules(country="US")
    assert is_extension("101", au) and is_extension("7999", au)
    assert not is_extension("1223", au) and not is_extension("988", us)
    assert refused("1223", au) and refused("988", us)
    # So they get no approval waiver and no pass on calling hours.
    cfg = dataclasses.replace(make_config(tmp_path), call_hours=("09:00", "17:00"), call_approval="new_numbers",
                              dial_rules=dataclasses.replace(au, allow_pattern="1223"))
    manager = CallManager(cfg, Database(cfg.data_dir), FakeSpeech(), FakeDialog(), None)
    night = datetime(2026, 11, 26, 23, 0, tzinfo=ZoneInfo("Australia/Sydney")).timestamp()
    assert manager.hours_error("1223", "Australia/Sydney", False, night)
    assert manager.hours_error("101", "Australia/Sydney", False, night) is None
    assert manager.auto_approval("1223") is None and manager.auto_approval("101") == "internal extension"


def test_call_hours_are_checked_and_padded():
    assert _call_hours("8:30-18:30") == ("08:30", "18:30")
    assert _call_hours(" 08:30 - 18:30 ") == ("08:30", "18:30")
    assert _call_hours("22:00-02:00") == ("22:00", "02:00")
    for bad in ["08:30", "8.30-18.30", "25:00-26:00", "08:61-09:00", "nine-five"]:
        with pytest.raises(ValueError):
            _call_hours(bad)


# Second batch: where heard lines stand, the PBX token, approval page, statuses


def test_first_since_press_ignores_the_menu_tail_and_later_lines(tmp_path):
    session = make_session(tmp_path)
    session._since_press, session._last_heard = 0, ""  # we just pressed a key
    session._before = [True]  # the menu's last sentence, said before the press
    assert session._where_heard(["For sales, press one."]) == (True, False, False)
    session._before = [False]
    assert session._where_heard(["Sales, Jordan speaking."]) == (False, True, False)
    session._before = [False]  # whatever comes next is no longer the first thing
    assert session._where_heard(["It'll arrive in about three weeks."])[1] is False


async def test_after_tones_follows_what_was_heard_not_the_transcript_tail(tmp_path):
    session = make_session(tmp_path)
    session._before = [False]
    assert session._where_heard(["Beep. Beep."])[2] is False
    session._before = [False]
    assert session._where_heard(["Hi, we're with Riverside."])[2] is True
    await session._say("Hello there.")  # we spoke: tones before that no longer count
    session._before = [False]
    assert session._where_heard(["Hi again."]) == (False, False, False)


def web_client(tmp_path, **changes):
    from starlette.testclient import TestClient

    from phone_agent.auth import TokenAuth
    from phone_agent.server import build_app
    from test_approval import manager_for

    manager, _ = manager_for(tmp_path, api_token="api", approval_token="approve-me", **changes)
    mcp = build_app(manager.cfg, manager, manager.db, None, None, None)
    return manager, TestClient(TokenAuth(mcp.streamable_http_app(), "api", "approve-me"), follow_redirects=False)


def test_inbound_takes_its_token_in_the_form_body(tmp_path):
    manager, client = web_client(tmp_path)
    form = {"uuid": str(uuid.uuid4()), "caller": "0491570156"}
    refused_reply = client.post("/inbound", data=form)
    assert refused_reply.status_code == 401
    # The dialplan compares the reply inside quotes: a quote in it breaks the comparison.
    assert refused_reply.text == "wrong token" and '"' not in refused_reply.text
    assert client.post("/inbound", data=form | {"token": "nope"}).status_code == 401
    assert client.post("/inbound", data=form | {"token": "api"}).text == "ok"
    # The header still works, and a body token opens nothing else.
    other = {"uuid": str(uuid.uuid4()), "caller": "0491570156"}
    assert client.post("/inbound", data=other, headers={"Authorization": "Bearer api"}).status_code in (200, 409)
    assert client.post("/calls/x/approve", data={"token": "approve-me"}).status_code == 401


def test_inbound_cannot_replace_an_ended_call(tmp_path):
    manager, client = web_client(tmp_path)
    old = str(uuid.uuid4())
    manager.db.save_call({"id": old, "created": 0, "phone": "0255502222", "callee": "Riverside Motel", "brief": {},
                          "status": "finished", "transcript": [["callee", "evidence"]]})
    reply = client.post("/inbound", data={"uuid": old, "caller": "x"}, headers={"Authorization": "Bearer api"})
    assert reply.status_code == 409
    assert manager.db.get_call(old)["transcript"] == [["callee", "evidence"]]


def test_login_only_redirects_into_this_site(tmp_path):
    _, client = web_client(tmp_path)
    for target in ["/%5Cevil.example/calls/x", "//evil.example", "https://evil.example", "/%0d%0aSet-Cookie:x=1"]:
        reply = client.post(f"/login?next={target}", data={"token": "approve-me"})
        assert reply.status_code == 303 and reply.headers["location"] == "/"
    assert client.post("/login?next=/calls/abc", data={"token": "approve-me"}).headers["location"] == "/calls/abc"


def test_approval_page_shows_the_whole_brief(tmp_path):
    manager, client = web_client(tmp_path, call_approval="all")
    brief = Brief(goal="Find the price", callee="Hotel", opening_line=OPENING, questions={"price": "price per night?"},
                  must_not=["mention the gate code"], recording_notice=True)

    async def place():
        return manager.start_call("0255500000", brief, max_seconds=7200)

    loop = asyncio.new_event_loop()
    call = loop.run_until_complete(place())
    page = client.get(f"/calls/{call.id}", headers={"Authorization": "Bearer approve-me"}).text
    loop.close()
    for shown in ["mention the gate code", "Time limit: 120 minutes", "Find the price", "<b>price:</b>",
                  "Says the call is recorded"]:
        assert shown in page


async def test_a_call_stopped_before_anyone_answered_is_cancelled_not_finished(tmp_path):
    warming = asyncio.Event()
    release = asyncio.Event()

    class Dialog(FakeDialog):
        async def warm_up(self, brief=None):
            warming.set()
            await release.wait()

    cfg = make_config(tmp_path)
    manager = CallManager(cfg, Database(cfg.data_dir), FakeSpeech(), Dialog(), None)
    call = manager.start_call("0255501234", BRIEF)
    await asyncio.wait_for(warming.wait(), 5)
    assert await manager.hang_up(call.id)
    release.set()
    await asyncio.wait_for(call.done.wait(), 5)
    assert (call.status, call.outcome) == ("cancelled", "hung_up_by_owner")
    assert manager.get(call.id)["status"] == "cancelled"


async def test_stopping_the_conversation_takes_the_model_turn_with_it(tmp_path):
    started = asyncio.Event()
    finished = []

    class Slow(FakeDialog):
        async def next_turn(self, *args, **kwargs):
            started.set()
            await asyncio.sleep(1)
            finished.append(True)  # would write to a call that is already wrapped up
            return await super().next_turn(*args, **kwargs)

    session = make_session(tmp_path, dialog=Slow())
    await session._say("Do you have a room?")
    turn = asyncio.create_task(session._turn_with_filler("They spoke.", they_spoke=False))
    await asyncio.wait_for(started.wait(), 5)
    turn.cancel()  # the watchdog stops the call
    with pytest.raises(asyncio.CancelledError):
        await turn
    await asyncio.sleep(1.3)
    assert finished == []
