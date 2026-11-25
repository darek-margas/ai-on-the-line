"""What happens when the infrastructure misbehaves: hung Wyoming servers, late
or silent AudioSocket connections, lost ARI replies, cancellations in the queue."""

import asyncio
import dataclasses
import time
import uuid
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from phone_agent import audiosocket as ass
from phone_agent import calls as calls_mod
from phone_agent import speech as speech_mod
from phone_agent.calls import CallManager, CallState
from phone_agent.db import Database
from phone_agent.dialog import Brief
from phone_agent.speech import Speech, SpeechTimeout

from test_call_flow import FakeDialog, FakeSpeech, make_config

BRIEF = Brief(goal="g", callee="c", opening_line="Hi.", questions={"q": "q?"})


async def silent_server():
    """Accepts connections and never answers, like a stuck Whisper or Piper."""
    async def handle(reader, writer):
        await reader.read()  # until the client gives up

    return await asyncio.start_server(handle, "127.0.0.1", 0)


async def test_stuck_wyoming_server_times_out(monkeypatch):
    monkeypatch.setattr(speech_mod, "TTS_TIMEOUT_SECONDS", 0.2)
    monkeypatch.setattr(speech_mod, "STT_TIMEOUT_SECONDS", 0.2)
    server = await silent_server()
    addr = ("127.0.0.1", server.sockets[0].getsockname()[1])
    speech = Speech(addr, addr, "en", "")
    try:
        with pytest.raises(SpeechTimeout):
            await speech.synthesize("Hello.")
        with pytest.raises(SpeechTimeout):
            await speech.transcribe(bytes(320))
    finally:
        server.close()


async def test_stuck_piper_gives_the_inbound_slot_back(tmp_path, monkeypatch):
    monkeypatch.setattr(speech_mod, "TTS_TIMEOUT_SECONDS", 0.2)
    server = await silent_server()
    addr = ("127.0.0.1", server.sockets[0].getsockname()[1])
    cfg = make_config(tmp_path)
    manager = CallManager(cfg, Database(cfg.data_dir), Speech(addr, addr, "en", ""), FakeDialog(), None)
    try:
        with pytest.raises(SpeechTimeout):
            await manager.register_inbound(str(uuid.uuid4()), "0255501234", "", "screener")
        manager.speech = FakeSpeech()  # Piper is back: the next caller gets through, not "busy"
        call = await manager.register_inbound(str(uuid.uuid4()), "0255501234", "", "screener")
        assert call.status == "ringing"
    finally:
        server.close()


async def test_tts_cache_is_bounded_and_keeps_what_is_used(monkeypatch):
    monkeypatch.setattr(speech_mod, "CACHE_MAX_BYTES", 3000)
    speech = Speech(("x", 1), ("x", 1), "en", "")
    rendered = []

    async def fake(text):
        rendered.append(text)
        return bytes(1000)

    speech._synthesize = fake
    for text in ["a", "b", "c"]:
        await speech.synthesize(text)
    await speech.synthesize("a")  # used again: now the newest
    await speech.synthesize("d")  # pushes out the oldest, "b"
    assert list(speech._tts_cache) == ["c", "a", "d"] and speech._cache_bytes == 3000
    await speech.synthesize("a")
    assert rendered == ["a", "b", "c", "d"]  # "a" never rendered twice


async def audiosocket_reply(manager, call_id: str | None, monkeypatch=None) -> list[int]:
    """Connect like Asterisk and return the message kinds the agent sends before closing."""
    server = await asyncio.start_server(manager.handle_audiosocket, "127.0.0.1", 0)
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", server.sockets[0].getsockname()[1])
        if call_id:
            writer.write(ass.encode(ass.KIND_UUID, uuid.UUID(call_id).bytes))
        kinds = []
        try:
            while True:
                kind, _ = await asyncio.wait_for(ass.read_message(reader), 3)
                kinds.append(kind)
        except asyncio.IncompleteReadError:
            pass  # closed by the agent
        writer.close()
        return kinds
    finally:
        server.close()


@pytest.mark.parametrize("status", ["failed", "not_answered", "cancelled", "queued"])
async def test_late_audiosocket_does_not_revive_a_call(tmp_path, status):
    cfg = make_config(tmp_path)
    manager = CallManager(cfg, Database(cfg.data_dir), FakeSpeech(), FakeDialog(), None)
    call = CallState(id=str(uuid.uuid4()), phone="0255501234", brief=BRIEF, owner="Alex", db=manager.db)
    call.status = status
    manager.calls[call.id] = call
    assert await audiosocket_reply(manager, call.id) == [ass.KIND_HANGUP]
    assert call.status == status and not call.connected.is_set()


async def test_silent_audiosocket_connection_is_dropped(tmp_path, monkeypatch):
    monkeypatch.setattr(calls_mod, "AUDIOSOCKET_HELLO_SECONDS", 0.2)
    cfg = make_config(tmp_path)
    manager = CallManager(cfg, Database(cfg.data_dir), FakeSpeech(), FakeDialog(), None)
    t0 = time.monotonic()
    assert await audiosocket_reply(manager, None) == []
    assert time.monotonic() - t0 < 2


class RecordingAri:
    def __init__(self, originate_error=None, exists_error=None):
        self.originate_error, self.exists_error = originate_error, exists_error
        self.channel_ids, self.hangups = [], []

    async def originate(self, endpoint, call_id, caller_id, audiosocket, timeout, channel_id=None):
        self.channel_ids.append(channel_id)
        if self.originate_error:
            raise self.originate_error
        return channel_id

    async def channel_exists(self, channel_id):
        if self.exists_error:
            raise self.exists_error
        return False

    async def hangup(self, channel_id):
        self.hangups.append(channel_id)


async def test_lost_originate_reply_still_hangs_up_the_channel(tmp_path):
    cfg = make_config(tmp_path)
    ari = RecordingAri(originate_error=ConnectionError("reply lost"))
    manager = CallManager(cfg, Database(cfg.data_dir), FakeSpeech(), FakeDialog(), ari)
    call = manager.start_call("0255501234", BRIEF)
    await asyncio.wait_for(call.done.wait(), 10)
    (chosen,) = ari.channel_ids
    assert uuid.UUID(chosen) and ari.hangups == [chosen]
    assert call.status == "failed"


async def test_failure_after_dialling_hangs_up_and_forgets_the_call(tmp_path):
    cfg = make_config(tmp_path)
    ari = RecordingAri(exists_error=RuntimeError("ARI down"))
    manager = CallManager(cfg, Database(cfg.data_dir), FakeSpeech(), FakeDialog(), ari)
    call = manager.start_call("0255501234", BRIEF)
    await asyncio.wait_for(call.done.wait(), 20)
    assert ari.hangups == ari.channel_ids and call.status == "failed"
    assert call.id not in manager.calls  # ended calls live in SQLite only
    assert manager.get(call.id)["status"] == "failed"


async def test_call_cancelled_in_the_queue_gives_up_without_warming_up(tmp_path):
    first_dialling = asyncio.Event()
    release = asyncio.Event()

    class Dialog(FakeDialog):
        warm_ups = 0

        async def warm_up(self, brief=None):
            Dialog.warm_ups += 1
            first_dialling.set()
            await release.wait()

    cfg = make_config(tmp_path)
    manager = CallManager(cfg, Database(cfg.data_dir), FakeSpeech(), Dialog(), RecordingAri())
    first = manager.start_call("0255501234", BRIEF)
    await asyncio.wait_for(first_dialling.wait(), 5)
    second = manager.start_call("0255501235", BRIEF)
    await asyncio.sleep(0.1)
    assert second.status == "queued"
    assert await manager.hang_up(second.id)
    await asyncio.wait_for(second.done.wait(), 2)  # without waiting for the line
    assert Dialog.warm_ups == 1
    release.set()
    await asyncio.wait_for(first.done.wait(), 10)
    assert manager._slots._value == 1  # the line is free again


def test_calling_hours_use_the_extension_rules_and_can_cross_midnight(tmp_path):
    cfg = make_config(tmp_path)
    cfg = dataclasses.replace(cfg, call_hours=("22:00", "02:00"),
                              dial_rules=dataclasses.replace(cfg.dial_rules, extensions=r"7\d{3,5}"))
    manager = CallManager(cfg, Database(cfg.data_dir), FakeSpeech(), FakeDialog(), None)
    zone = "Australia/Sydney"

    def at(hour):
        return datetime(2026, 11, 26, hour, 0, tzinfo=ZoneInfo(zone)).timestamp()

    assert manager.hours_error("0255501234", zone, False, at(23)) is None
    assert manager.hours_error("0255501234", zone, False, at(1)) is None
    assert "calling hours are 22:00-02:00" in manager.hours_error("0255501234", zone, False, at(12))
    assert manager.hours_error("799912", zone, False, at(12)) is None  # a 6-digit extension
    assert manager.hours_error("13 22 00", zone, False, at(12))  # short, but not an extension


async def test_ari_failing_after_the_audio_connects_keeps_the_call(tmp_path, monkeypatch):
    """The PBX dialled and the call is up, but the originate reply never came."""
    import phone_agent.vad as vad
    from test_call_flow import FakeAri

    monkeypatch.setattr(vad.webrtcvad.Vad, "is_speech", lambda self, frame, rate: max(frame) > 8)
    cfg = make_config(tmp_path)
    manager = CallManager(cfg, Database(cfg.data_dir), FakeSpeech(), FakeDialog(), None)
    server = await asyncio.start_server(manager.handle_audiosocket, "127.0.0.1", 0)

    class LateFailingAri(FakeAri):
        hangups = []

        async def originate(self, endpoint, call_id, caller_id, audiosocket, timeout, channel_id=None):
            await super().originate(endpoint, call_id, caller_id, audiosocket, timeout, channel_id)
            call = manager.calls[call_id]
            await asyncio.wait_for(call.connected.wait(), 5)
            raise ConnectionError("ARI reply lost")

        async def hangup(self, channel_id):
            self.hangups.append(channel_id)

    ari = LateFailingAri(server.sockets[0].getsockname()[1])
    manager.ari = ari
    try:
        call = manager.start_call("0255501234", BRIEF)
        await asyncio.wait_for(call.connected.wait(), 10)
        await asyncio.sleep(0.2)
        assert call.id in manager.calls and manager._slots._value == 0  # still ours, line still held
        await asyncio.wait_for(call.done.wait(), 30)
        await asyncio.wait_for(ari.task, 10)
    finally:
        server.close()
    assert call.conversation_over.is_set()  # wrapped up only after the conversation
    assert call.status == "finished" and call.transcript
    assert manager._slots._value == 1


async def test_cancelled_while_winning_a_line_gives_it_back(tmp_path):
    cfg = make_config(tmp_path)
    manager = CallManager(cfg, Database(cfg.data_dir), FakeSpeech(), FakeDialog(), None)
    call = CallState(id=str(uuid.uuid4()), phone="0255501234", brief=BRIEF, owner="Alex", db=manager.db)
    await manager._slots.acquire()  # another call holds the only line
    waiting = asyncio.create_task(manager._take_slot(call))
    await asyncio.sleep(0.05)
    manager._slots.release()  # the line comes free: the acquire wins it...
    waiting.cancel()  # ...in the same moment as we're cancelled
    with pytest.raises(asyncio.CancelledError):
        await waiting
    assert manager._slots._value == 1


async def test_cancelled_while_waiting_for_a_line_withdraws(tmp_path):
    cfg = make_config(tmp_path)
    manager = CallManager(cfg, Database(cfg.data_dir), FakeSpeech(), FakeDialog(), None)
    call = CallState(id=str(uuid.uuid4()), phone="0255501234", brief=BRIEF, owner="Alex", db=manager.db)
    await manager._slots.acquire()
    waiting = asyncio.create_task(manager._take_slot(call))
    await asyncio.sleep(0.05)
    waiting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiting
    manager._slots.release()
    await asyncio.sleep(0)
    assert manager._slots._value == 1


async def test_tts_cache_skips_audio_bigger_than_the_cache(monkeypatch):
    monkeypatch.setattr(speech_mod, "CACHE_MAX_BYTES", 3000)
    speech = Speech(("x", 1), ("x", 1), "en", "")

    async def fake(text):
        return bytes(4000 if text == "long" else 1000)

    speech._synthesize = fake
    await speech.synthesize("a")
    await speech.synthesize("long")
    assert list(speech._tts_cache) == ["a"] and speech._cache_bytes == 1000


def test_calling_hours_clean_up_the_number_like_the_dialler(tmp_path):
    cfg = make_config(tmp_path)
    cfg = dataclasses.replace(cfg, call_hours=("09:00", "17:00"),
                              dial_rules=dataclasses.replace(cfg.dial_rules, extensions=r"7\d{3}"))
    manager = CallManager(cfg, Database(cfg.data_dir), FakeSpeech(), FakeDialog(), None)
    night = datetime(2026, 11, 26, 23, 0, tzinfo=ZoneInfo("Australia/Sydney")).timestamp()
    assert calls_mod.normalize_number("7.123", cfg.dial_rules) == "7123"
    assert manager.hours_error("7.123", "Australia/Sydney", False, night) is None
