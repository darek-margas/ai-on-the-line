# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Darek Margas. See NOTICE.
"""End-to-end call flow with fake Asterisk, speech and LLM (no network, no PBX)."""

import asyncio
import uuid

import pytest

from phone_agent import audiosocket as ass
from phone_agent.calls import CallManager
from phone_agent.dialing import DialRules, normalize_number
from phone_agent.config import Config
from phone_agent.db import Database
from phone_agent.dialog import Brief, Turn, turn_schema

SPEECH = b"\x10\x27" * 160  # one loud 20 ms frame
SILENCE = b"\x00\x00" * 160


class FakeSpeech:
    rate = 16000  # the agent side runs wideband; fake Asterisk sends 8 kHz

    async def synthesize(self, text: str) -> bytes:
        return bytes(640) * 5  # 100 ms of "speech" at 16 kHz

    async def transcribe(self, pcm: bytes) -> str:
        return "Yes, we have a family room for two hundred dollars."


class FakeDialog:
    def __init__(self):
        self.calls = 0

    async def warm_up(self, brief=None):
        pass

    async def next_turn(self, brief, transcript, answers, event, notes=None, on_sentence=None):
        self.calls += 1
        return Turn(
            say="Thank you, that's all I needed. Goodbye.",
            answers={"price": "two hundred dollars"},
            notes="family room",
            end_call=True,
            outcome="completed",
        )


class FakeAri:
    """Originate 'answers' immediately by connecting to AudioSocket like Asterisk would."""

    def __init__(self, port: int):
        self.port = port
        self.received: list[int] = []
        self.task = None

    async def originate(self, endpoint, call_id, caller_id, audiosocket, timeout):
        self.task = asyncio.create_task(self._asterisk(call_id))
        return "chan-1"

    async def _asterisk(self, call_id: str):
        reader, writer = await asyncio.open_connection("127.0.0.1", self.port)
        writer.write(ass.encode(ass.KIND_UUID, uuid.UUID(call_id).bytes))

        async def listen():
            while True:
                kind, _ = await ass.read_message(reader)
                self.received.append(kind)
                if kind == ass.KIND_HANGUP:
                    return

        listener = asyncio.create_task(listen())
        # "Hello, hotel reception" then silence, at real-time pace.
        for frame in [SILENCE] * 10 + [SPEECH] * 25 + [SILENCE] * 300:
            if listener.done():
                break
            writer.write(ass.encode(ass.KIND_AUDIO, frame))
            await writer.drain()
            await asyncio.sleep(0.02)
        await listener
        writer.close()

    async def channel_exists(self, channel_id):
        return True

    async def hangup(self, channel_id):
        pass

    async def send_dtmf(self, channel_id, digits):
        pass


def make_config(tmp_path) -> Config:
    return Config(
        data_dir=str(tmp_path), http_host="127.0.0.1", http_port=0,
        audiosocket_bind=("127.0.0.1", 0), audiosocket_advertise="127.0.0.1:0", audio_rate=16000,
        ari_url="http://x", ari_user="u", ari_password="p", dial_endpoint="Local/{number}@from-internal/n",
        caller_id="", ami_url="", ami_user="", ami_password="", ring_timeout_seconds=10, stt=("x", 1), stt_language="en", tts=("x", 1), tts_voice="", tts_pronounce={},
        dialog_llm="claude:x", ollama_url="", ollama_keep_alive="-1", owner_name="Alex", timezone="Australia/Sydney",
        call_hours=("00:00", "23:59"), max_concurrent_calls=1, max_call_seconds=60,
        hold_timeout_seconds=30, barge_in=True, recordings_dir="",
    )


@pytest.mark.asyncio
async def test_call_completes(tmp_path):
    cfg = make_config(tmp_path)
    db = Database(cfg.data_dir)
    dialog = FakeDialog()
    manager = CallManager(cfg, db, FakeSpeech(), dialog, None)
    server = await asyncio.start_server(manager.handle_audiosocket, "127.0.0.1", 0)
    ari = FakeAri(server.sockets[0].getsockname()[1])
    manager.ari = ari

    # Treat any non-silent frame as speech so the test doesn't depend on WebRTC VAD.
    import phone_agent.vad as vad
    orig = vad.webrtcvad.Vad.is_speech
    vad.webrtcvad.Vad.is_speech = lambda self, frame, rate: max(frame) > 8  # resampled silence stays ~0
    try:
        brief = Brief(goal="Ask price", callee="Hotel", opening_line="Hi, automated assistant here.",
                      questions={"price": "price per night"})
        call = manager.start_call("+61 8 5550 1234", brief)
        await asyncio.wait_for(call.done.wait(), 30)
        await asyncio.wait_for(ari.task, 10)  # fake Asterisk has seen the hangup
    finally:
        vad.webrtcvad.Vad.is_speech = orig
        server.close()

    saved = db.get_call(call.id)
    assert saved["status"] == "finished"
    assert saved["outcome"] == "completed"
    assert saved["answers"] == {"price": "two hundred dollars"}
    assert saved["notes"] == ["family room"]
    speakers = [who for who, _ in saved["transcript"]]
    # A short first utterance is a person answering: the pre-rendered opening
    # plays at once, then the dialog takes over.
    assert speakers == ["callee", "agent", "agent"]
    assert saved["transcript"][1][1] == brief.opening_line
    assert ass.RATE_KINDS[16000] in ari.received and ari.received[-1] == ass.KIND_HANGUP
    assert dialog.calls == 1


def test_normalize_number_australia():
    sydney = DialRules(local_area_code="02")
    n = lambda phone: normalize_number(phone, sydney)
    assert n("+61 8 5550 1234") == "0855501234"
    assert n("(08) 5550 1234") == "0855501234"
    assert n("61855501234") == "0855501234"
    assert n("100") == "100"  # internal extension
    assert n("0491 570 156") == "0491570156"
    assert n("5550 1234") == "55501234"  # local, area code from DIAL_LOCAL_AREA_CODE
    assert n("1800 123 456") == "1800123456"
    assert n("101") == "101" and n("7999") == "7999"
    for refused in ("call me", "", "13 13 13", "131313", "1300 123 456", "000", "112", "106", "1900 123 456",
                    "+48 123 456 789", "+44 20 7946 0958", "5550 123"):
        with pytest.raises(ValueError):
            n(refused)
    with pytest.raises(ValueError):
        normalize_number("5550 1234")  # no local area code set


def test_normalize_number_other_countries_and_overrides():
    uk = DialRules(country="GB")
    assert normalize_number("020 7946 0958", uk) == "02079460958"
    assert normalize_number("07400 123456", uk) == "07400123456"
    for refused in ("999", "112", "09065 123456"):  # emergency, premium
        with pytest.raises(ValueError):
            normalize_number(refused, uk)
    assert normalize_number("+44 20 7946 0958", DialRules(allow_international=True)) == "0011442079460958"
    assert normalize_number("0491 570 156", DialRules(dial_format="e164")) == "+61491570156"
    assert normalize_number("13 13 13", DialRules(allow={"shared_cost"})) == "131313"
    with pytest.raises(ValueError):
        normalize_number("0491 570 156", DialRules(block_pattern=r"04\d+"))
    # Emergency numbers are refused even when the settings would allow everything.
    for emergency in ("000", "112", "911"):
        with pytest.raises(ValueError):
            normalize_number(emergency, DialRules(allow_pattern=r"\+?\d+", extensions=r"\d+"))


def test_framing_roundtrip():
    msg = ass.encode(ass.KIND_AUDIO, b"\x01\x02")
    assert msg == b"\x10\x00\x02\x01\x02"
    frames = ass.split_frames(b"\x01" * 500, 8000)
    assert [len(f) for f in frames] == [320, 320]
    assert ass.frame_bytes(16000) == 640 and ass.RATE_KINDS[16000] == 0x12


def test_schema_is_strict():
    schema = turn_schema(["price", "availability"])
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == set(schema["properties"])


def test_placeholder_answers_are_dropped():
    from phone_agent.dialog import is_real_answer

    assert not is_real_answer("Unclear; callee could not hear")
    assert not is_real_answer("not answered")
    assert not is_real_answer("N/A")
    assert is_real_answer("None of the rooms have breakfast")
    assert is_real_answer("210 dollars a night")


def test_question_names_from_form_lines():
    from phone_agent.server import question_names

    names = question_names(["What is your favourite colour?", "What is your favourite animal?", "What is it?"])
    assert list(names) == ["favourite_colour", "favourite_animal", "question"]


def test_echo_of_our_own_line_is_recognised():
    from phone_agent.session import is_echo

    ours = "Quick one more: what's your favourite plant for a big outdoor pot?"
    assert is_echo("favourite plant for a big pot", ours)
    assert not is_echo("Sorry, we don't sell pots that big", ours)
    assert not is_echo("Yeah, a lemon tree", ours)


def test_first_words_classification():
    from phone_agent.session import asks_how_we_are, opening_lines, person_greeting

    assert person_greeting("Flower Power, Jordan speaking.", 1.8)
    assert person_greeting("Hello, how are you?", 1.2)
    assert not person_greeting("Thank you for calling Flower Power Riverside.", 3.5)
    assert not person_greeting("For click and collect, press 1.", 2.0)
    assert not person_greeting("Hi, you've reached Sam", 6.0)  # long: likely a recording
    lines = opening_lines("Hi, I'm Alex's assistant.")
    assert lines[asks_how_we_are("Hello, how are you?")].startswith("I'm good")
    assert lines[asks_how_we_are("Hello?")] == "Hi, I'm Alex's assistant."


def test_split_sentences():
    from phone_agent.session import split_sentences

    assert split_sentences("Got it, thanks. Next one: what colour?") == ["Got it, thanks.", "Next one: what colour?"]
    assert split_sentences("Hello") == ["Hello"]


def test_acknowledgements():
    from phone_agent.session import is_acknowledgement

    for ack in ("Okay, go ahead.", "Yeah, sure.", "Mm.", "Go on", "Yep, yep."):
        assert is_acknowledgement(ack), ack
    assert is_acknowledgement("Thank you.") and is_acknowledgement("Great, thanks!")
    for answer in ("Yes, they do.", "Yeah, a lemon tree", "Sure, two hundred dollars", "No."):
        assert not is_acknowledgement(answer), answer


def test_unfinished_sentences():
    from phone_agent.session import sounds_unfinished

    assert sounds_unfinished("Okay, so you would have to call my wife on which I type into the")
    assert sounds_unfinished("We have a family room, and")
    assert sounds_unfinished("Let me check with the yard and")
    assert not sounds_unfinished("Yes, we deliver on Tuesdays.")
    assert not sounds_unfinished("Is that for the whole room?")


def test_listening_vs_thank_you():
    from phone_agent.session import is_acknowledgement, is_listening

    assert is_listening("Okay, go ahead.") and is_listening("Yeah, sure.")
    # A short answer can come out of Whisper as "Thank you.": not "listening".
    assert not is_listening("Thank you.") and is_acknowledgement("Thank you.")


def test_person_cues():
    from phone_agent.session import PERSON_CUE

    for said in ("We've been in the plant business since 1968... Landscape, John speaking.", "Say it again.", "Hello?", "How can I help?"):
        assert PERSON_CUE.search(said), said
    for recording in ("For click and collect press 1.", "Please hold for any other inquiries."):
        assert not PERSON_CUE.search(recording), recording


def test_reception_greeting_is_a_person():
    from phone_agent.session import person_greeting

    assert person_greeting("Good morning, how are you? Harvey Norman Riverside, Josh speaking. How may I direct your call?", 5.5)
    assert not person_greeting("Thank you for calling Flower Power Riverside. We are open until 5.30pm today.", 5.5)


def test_answered_after_menu():
    from phone_agent.session import answered_after_menu

    assert answered_after_menu("Have you no more, Pak?")
    assert answered_after_menu("Whitegoods, Sam speaking.")
    for menu_talk in ("Press 5 to return to the previous menu.", "Please hold while we transfer your call.",
                      "Thank you.", "Beep.", "Ring ring.", "We've been in the plant business since 1968, so we know a thing or two about gardening."):
        assert not answered_after_menu(menu_talk), menu_talk


def test_pronunciation_spellings():
    from phone_agent.speech import Speech

    s = Speech(("x", 1), ("x", 1), "en", "", 16000, {"Siobhan": "Shivawn"})
    assert s._spoken("Hi, I'm Siobhan's assistant.") == "Hi, I'm Shivawn's assistant."
    assert s._spoken("Siobhana") == "Siobhana"  # whole words only


def test_contacts_from_vcard():
    import importlib.util, pathlib

    spec = importlib.util.spec_from_file_location("c2f", pathlib.Path(__file__).parent.parent / "tools" / "contacts_to_freepbx.py")
    c2f = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(c2f)
    vcf = (
        "BEGIN:VCARD\r\nVERSION:3.0\r\nN:Smith;Anna;;;\r\nFN:Anna Smith\r\n"
        "item1.TEL;type=CELL;type=pref:+61 491 570 156\r\nTEL;type=HOME:(02) 5550 1234\r\n"
        "TEL;type=WORK;type=FAX:02 5550 9999\r\nEND:VCARD\r\n"
        "BEGIN:VCARD\r\nVERSION:3.0\r\nN:;;;;\r\nORG:Joe\\, Plumbing;\r\nTEL:5550 5466\r\nTEL:+48 12 345 67 89\r\nEND:VCARD\r\n"
        "BEGIN:VCARD\r\nFN:Long\r\n Name\r\nTEL;type=WORK:0011 61 491 570 158\r\nTEL:0491 570 156\r\nEND:VCARD\r\n"
    )
    entries = c2f.contacts(vcf)
    assert entries == [
        {"displayname": "Anna Smith", "fname": "Anna", "lname": "Smith", "company": "", "title": "",
         "numbers": [{"number": "0491570156", "type": "cell"}, {"number": "0255501234", "type": "home"}]},  # no fax
        {"displayname": "Joe, Plumbing", "fname": "", "lname": "", "company": "Joe, Plumbing", "title": "",
         "numbers": [{"number": "0255505466", "type": "other"}]},  # Sydney local; Polish number left out
        {"displayname": "LongName", "fname": "", "lname": "", "company": "", "title": "",
         "numbers": [{"number": "0491570158", "type": "work"}]},  # folded FN; 0011 61; Anna's number not repeated
    ]
    # Blocked contacts (by name, or by any of their numbers in any format) are left out.
    assert [e["displayname"] for e in c2f.contacts(vcf, ["anna smith", "+61 2 5550 5466"])] == ["LongName"]


def test_laughs_become_hey():
    from phone_agent.dialog import soften_laughs

    assert soften_laughs("Ha, honest, I like that! What kind of product is it?") == "Hey, honest, I like that! What kind of product is it?"
    assert soften_laughs("Haha! Bold claim.") == "Hey! Bold claim."
    assert soften_laughs("Ha ha, fair enough.") == "Hey, fair enough."
    assert soften_laughs("Got it. Ha, the President himself!") == "Got it. Hey, the President himself!"
    assert soften_laughs("That's half the price, ha.") == "That's half the price."
    assert soften_laughs("The Hague office has a hall.") == "The Hague office has a hall."


def test_unfinished_and_hold_sounds():
    from phone_agent.session import HOLD_PHRASE, is_hold_sound, sounds_unfinished

    assert sounds_unfinished("I could... I think about...") and sounds_unfinished("I think it was just the…")
    assert sounds_unfinished("a little")
    from phone_agent.session import trails_off

    assert trails_off("I could... I think about...") and trails_off("We have a family room, and")
    assert not trails_off("breakfast is not included but there is a cafe next door")  # just no full stop
    assert is_hold_sound("Beep.  Beep.") and is_hold_sound("Music.") and not is_hold_sound("Thank you.")
    assert HOLD_PHRASE.search("No worries. Transferring you now.") and HOLD_PHRASE.search("Please hold.")
    assert not HOLD_PHRASE.search("We've got the Macro and the Capilano brands.")


class ScriptedSpeech(FakeSpeech):
    """Each utterance transcribes to the next line of the script."""

    def __init__(self, lines):
        self.lines = list(lines)

    async def transcribe(self, pcm: bytes) -> str:
        if not any(pcm):
            return ""  # the warm-up before dialling sends silence: not a script line
        return self.lines.pop(0) if self.lines else ""


class ScriptedDialog(FakeDialog):
    def __init__(self, turns):
        super().__init__()
        self.turns = list(turns)
        self.events = []

    async def next_turn(self, brief, transcript, answers, event, notes=None, on_sentence=None):
        self.calls += 1
        self.events.append(event)
        return self.turns.pop(0)


class BurstAri(FakeAri):
    """Fake Asterisk that plays bursts of 'speech' (frame counts) with pauses between."""

    def __init__(self, port: int, bursts, gaps=None):
        super().__init__(port)
        self.bursts = bursts
        self.gaps = gaps or [75] * len(bursts)  # silent frames after each burst

    async def _asterisk(self, call_id: str):
        reader, writer = await asyncio.open_connection("127.0.0.1", self.port)
        writer.write(ass.encode(ass.KIND_UUID, uuid.UUID(call_id).bytes))

        async def listen():
            while True:
                kind, _ = await ass.read_message(reader)
                self.received.append(kind)
                if kind == ass.KIND_HANGUP:
                    return

        listener = asyncio.create_task(listen())
        frames = [SILENCE] * 10
        for n, gap in zip(self.bursts, self.gaps):
            frames += [SPEECH] * n + [SILENCE] * gap
        for frame in frames + [SILENCE] * 300:
            if listener.done():
                break
            writer.write(ass.encode(ass.KIND_AUDIO, frame))
            await writer.drain()
            await asyncio.sleep(0.02)
        await listener
        writer.close()


@pytest.mark.asyncio
async def test_hold_queue_then_person(tmp_path):
    """Store robot, transfer, hold beeps, then a person: no replies to the beeps,
    and the person hears the pre-rendered opening at once (no dialog turn)."""
    cfg = make_config(tmp_path)
    db = Database(cfg.data_dir)
    speech = ScriptedSpeech([
        "Hey there, I'm your digital assistant. Tell me in a few words what you need.",
        "Okay, transferring you now, please hold.",
        "Beep.", "Beep.  Beep.",
        "Hi, we're with Riverside.",
        "No, sorry, there's no wallet here.",
    ])
    dialog = ScriptedDialog([
        Turn(say="I'm calling about a lost wallet."),
        Turn(),  # stays quiet while being transferred
        Turn(say="Okay, thanks anyway. Bye!", answers={"wallet_found": "no"}, end_call=True, outcome="completed"),
    ])
    manager = CallManager(cfg, db, speech, dialog, None)
    server = await asyncio.start_server(manager.handle_audiosocket, "127.0.0.1", 0)
    ari = BurstAri(server.sockets[0].getsockname()[1], [250, 25, 25, 25, 25, 25])
    manager.ari = ari

    import phone_agent.vad as vad
    orig = vad.webrtcvad.Vad.is_speech
    vad.webrtcvad.Vad.is_speech = lambda self, frame, rate: max(frame) > 8
    try:
        brief = Brief(goal="Lost wallet", callee="Store", opening_line="Hi, I'm Alex's assistant. Has a pink wallet been handed in?",
                      questions={"wallet_found": "was it handed in"})
        call = manager.start_call("0255500000", brief)
        await asyncio.wait_for(call.done.wait(), 60)
    finally:
        vad.webrtcvad.Vad.is_speech = orig
        server.close()

    saved = db.get_call(call.id)
    agent = [text for who, text in saved["transcript"] if who == "agent"]
    assert agent == ["I'm calling about a lost wallet.", brief.opening_line, "Okay, thanks anyway. Bye!"], saved["transcript"]
    assert dialog.calls == 3  # robot, transfer, the person's answer: none for the beeps or the greeting
    assert saved["outcome"] == "completed"


def test_say_streams_sentence_by_sentence():
    import json as _json

    from phone_agent.dialog import SayStream

    raw = _json.dumps({"say": 'Lovely, thanks! Where\'s "Macro" from?\nOr imported...', "dtmf": "", "end_call": False},
                      separators=(",", ":"))
    for step in (1, 2, 5, 11):  # however the stream happens to be chunked
        said, out = SayStream(), []
        for i in range(0, len(raw), step):
            out += said.feed(raw[i : i + step])
        assert out == ["Lovely, thanks!", 'Where\'s "Macro" from?', "Or imported..."], step
    assert SayStream().feed('{"say":"","dtmf":"1"}') == []  # silent turn: nothing to speak
    off = SayStream()
    assert off.feed('{"dtmf":"1","say":"Hi."}') == [] and off.off  # say not first: no streaming


def test_dialog_streams_say_from_claude():
    from phone_agent.dialog import Dialog

    dialog = Dialog.__new__(Dialog)
    dialog.owner, dialog.provider = "Alex", "claude"
    reply = '{"say":"Ha, great! Which brands?","dtmf":"","answers":[{"question":"q","answer":"yes"}],"notes":"","end_call":false,"outcome":"in_progress"}'

    async def fake_ask(system, user, schema, on_text=None):
        for i in range(0, len(reply), 4):
            await on_text(reply[i : i + 4])
        return reply

    dialog._ask_claude = fake_ask
    heard = []

    async def on_sentence(sentence):
        heard.append(sentence)

    brief = Brief(goal="g", callee="c", opening_line="Hi.", questions={"q": "q?"})
    turn = asyncio.run(dialog.next_turn(brief, [], {}, "event", on_sentence=on_sentence))
    assert heard == ["Hey, great!", "Which brands?"]  # laughs softened per sentence
    assert turn.spoken and turn.say == "Hey, great! Which brands?" and turn.answers == {"q": "yes"}


class StreamingDialog(FakeDialog):
    """Speaks its reply through on_sentence, like the streaming Claude path."""

    async def next_turn(self, brief, transcript, answers, event, notes=None, on_sentence=None):
        self.calls += 1
        for sentence in ("Thank you, that's all I needed.", "Goodbye."):
            await on_sentence(sentence)
        return Turn(say="Thank you, that's all I needed. Goodbye.", answers={"price": "two hundred dollars"},
                    end_call=True, outcome="completed", spoken=True)


@pytest.mark.asyncio
async def test_streamed_reply_is_spoken_once(tmp_path):
    cfg = make_config(tmp_path)
    db = Database(cfg.data_dir)
    dialog = StreamingDialog()
    speech = FakeSpeech()
    rendered = []
    orig_synth = speech.synthesize

    async def synthesize(text):
        rendered.append(text)
        return await orig_synth(text)

    speech.synthesize = synthesize
    manager = CallManager(cfg, db, speech, dialog, None)
    server = await asyncio.start_server(manager.handle_audiosocket, "127.0.0.1", 0)
    ari = FakeAri(server.sockets[0].getsockname()[1])
    manager.ari = ari

    import phone_agent.vad as vad
    orig = vad.webrtcvad.Vad.is_speech
    vad.webrtcvad.Vad.is_speech = lambda self, frame, rate: max(frame) > 8
    try:
        brief = Brief(goal="Ask price", callee="Hotel", opening_line="Hi, automated assistant here.",
                      questions={"price": "price per night"})
        call = manager.start_call("+61 8 5550 1234", brief)
        await asyncio.wait_for(call.done.wait(), 30)
    finally:
        vad.webrtcvad.Vad.is_speech = orig
        server.close()

    saved = db.get_call(call.id)
    assert saved["outcome"] == "completed"
    # One transcript line for the whole streamed reply, and each sentence rendered once.
    assert list(saved["transcript"][-1]) == ["agent", "Thank you, that's all I needed. Goodbye."]
    assert rendered.count("Goodbye.") == 1 and rendered.count("Thank you, that's all I needed.") == 1


def test_hesitations_and_open_questions():
    from phone_agent.session import FILLER_AFTER_SECONDS, HESITATION, asks_us, sounds_unfinished, trails_off

    # From a motel call: "Ah." then "You're looking for the manager or..."
    for hesitation in ("Ah.", "Um.", "Um, well...", "Hmm."):
        assert HESITATION.match(hesitation) and trails_off(hesitation) and sounds_unfinished(hesitation), hesitation
    for answer in ("Oh.", "Okay.", "No, I can't...", "Ah, yes we do."):
        assert not HESITATION.match(answer), answer
    assert asks_us("You're looking for the manager or...") and not sounds_unfinished("You're looking for the manager or...")
    assert asks_us("Do you want it or?")
    assert not asks_us("Doctor...") and sounds_unfinished("No, I can't...")
    assert FILLER_AFTER_SECONDS >= 1.5  # past where people carry on with their sentence


class SlowDialog(FakeDialog):
    """Takes a while to answer (like a real LLM) and streams its reply."""

    def __init__(self):
        super().__init__()
        self.seen = []

    async def next_turn(self, brief, transcript, answers, event, notes=None, on_sentence=None):
        self.calls += 1
        self.seen.append([text for who, text in transcript if who == "callee"])
        await asyncio.sleep(1.5)
        await on_sentence("Okay, thanks anyway. Bye!")
        return Turn(say="Okay, thanks anyway. Bye!", end_call=True, outcome="completed", spoken=True)


@pytest.mark.asyncio
async def test_carrying_on_replaces_the_pending_reply(tmp_path):
    """'The manager is away.' ... 'So I can't help you.': the first reply is dropped
    before it starts, and one reply answers both."""
    cfg = make_config(tmp_path)
    db = Database(cfg.data_dir)
    speech = ScriptedSpeech(["Hello?", "The manager is away.", "So I can't help you."])
    dialog = SlowDialog()
    manager = CallManager(cfg, db, speech, dialog, None)
    server = await asyncio.start_server(manager.handle_audiosocket, "127.0.0.1", 0)
    # Greeting, then two fragments 1 s apart: long enough to split, short enough
    # that the second starts while the first reply is still being worked out.
    ari = BurstAri(server.sockets[0].getsockname()[1], [25, 25, 25], gaps=[75, 50, 300])
    manager.ari = ari

    import phone_agent.vad as vad
    orig = vad.webrtcvad.Vad.is_speech
    vad.webrtcvad.Vad.is_speech = lambda self, frame, rate: max(frame) > 8
    try:
        brief = Brief(goal="Refund", callee="Motel", opening_line="Hi, it's about a cancelled booking. Got a moment?",
                      questions={"refund": "refund status"})
        call = manager.start_call("0255500000", brief)
        await asyncio.wait_for(call.done.wait(), 60)
    finally:
        vad.webrtcvad.Vad.is_speech = orig
        server.close()

    saved = db.get_call(call.id)
    agent = [text for who, text in saved["transcript"] if who == "agent"]
    assert agent == [brief.opening_line, "Okay, thanks anyway. Bye!"], saved["transcript"]
    assert dialog.calls == 2  # the first reply was dropped before any of it played
    assert dialog.seen[-1][-2:] == ["The manager is away.", "So I can't help you."]


@pytest.mark.asyncio
async def test_long_answer_is_not_silence(tmp_path, monkeypatch):
    """A long answer only becomes an utterance when it ends. Before that it
    mustn't count as silence: no "are you still there?" while they talk."""
    import phone_agent.session as session

    monkeypatch.setattr(session, "LISTEN_SECONDS", 1)
    monkeypatch.setattr(session, "THINKING_SECONDS", 2)
    cfg = make_config(tmp_path)
    db = Database(cfg.data_dir)
    speech = ScriptedSpeech(["Hello?", "Well, it depends on his budget, I sell everything from pots to cars."])
    dialog = ScriptedDialog([Turn(say="Got it, thanks. Bye!", end_call=True, outcome="completed")])
    manager = CallManager(cfg, db, speech, dialog, None)
    server = await asyncio.start_server(manager.handle_audiosocket, "127.0.0.1", 0)
    # A greeting, then a 4 s answer: twice the (shortened) thinking time.
    ari = BurstAri(server.sockets[0].getsockname()[1], [25, 200], gaps=[75, 300])
    manager.ari = ari

    import phone_agent.vad as vad
    orig = vad.webrtcvad.Vad.is_speech
    vad.webrtcvad.Vad.is_speech = lambda self, frame, rate: max(frame) > 8
    try:
        brief = Brief(goal="Pitch", callee="Caller", opening_line="Hi, it's about your offer. What do you sell?",
                      questions={"product": "what they sell"})
        call = manager.start_call("0255500000", brief)
        await asyncio.wait_for(call.done.wait(), 60)
    finally:
        vad.webrtcvad.Vad.is_speech = orig
        server.close()

    assert not any("No speech from the callee" in e for e in dialog.events), dialog.events
    assert dialog.calls == 1 and "just spoke" in dialog.events[0]


@pytest.mark.asyncio
async def test_no_reply_starts_while_they_talk(tmp_path):
    """'...found his company in the breach of' then a pause just past the
    trailing-off wait, then 'something, so I need to notify him': one reply
    after both parts, not one that starts while they're talking."""
    cfg = make_config(tmp_path)
    db = Database(cfg.data_dir)
    speech = ScriptedSpeech(["Hello?", "We found his company in the breach of...", "something, so I need to notify him."])
    dialog = SlowDialog()
    manager = CallManager(cfg, db, speech, dialog, None)
    server = await asyncio.start_server(manager.handle_audiosocket, "127.0.0.1", 0)
    # The second part starts ~1.2 s after the first ends and lasts 2 s, so the
    # 2.5 s wait for the rest of the first runs out while the second is under way.
    ari = BurstAri(server.sockets[0].getsockname()[1], [25, 50, 100], gaps=[75, 60, 300])
    manager.ari = ari

    import phone_agent.vad as vad
    orig = vad.webrtcvad.Vad.is_speech
    vad.webrtcvad.Vad.is_speech = lambda self, frame, rate: max(frame) > 8
    try:
        brief = Brief(goal="Screen", callee="Caller", opening_line="Hi, what's it about?", questions={"reason": "why"})
        call = manager.start_call("0255500000", brief)
        await asyncio.wait_for(call.done.wait(), 60)
    finally:
        vad.webrtcvad.Vad.is_speech = orig
        server.close()

    assert dialog.seen[0][-2:] == ["We found his company in the breach of...", "something, so I need to notify him."], dialog.seen


def test_only_a_plain_farewell_ends_the_call():
    from phone_agent.session import FAREWELL_ONLY

    for bye in ("Thanks, bye!", "Okay, cheers.", "No worries, you too. Bye bye.", "Thank you, have a good day."):
        assert FAREWELL_ONLY.match(bye), bye
    for more in ("Thanks, but before you go, the price excludes delivery.", "Bye, oh wait, one more thing.", "Cheers, and it's ninety dollars."):
        assert not FAREWELL_ONLY.match(more), more


@pytest.mark.asyncio
async def test_cancelled_queued_call_never_dials(tmp_path):
    """hang_up on a call that hasn't dialled yet stops it for good."""

    class SlowWarmUp(FakeDialog):
        async def warm_up(self, brief=None):
            await asyncio.sleep(1)

    cfg = make_config(tmp_path)
    db = Database(cfg.data_dir)
    manager = CallManager(cfg, db, FakeSpeech(), SlowWarmUp(), None)
    dialled = []

    class CountingAri(FakeAri):
        async def originate(self, *args, **kwargs):
            dialled.append(args)
            return "chan-1"

    manager.ari = CountingAri(0)
    brief = Brief(goal="g", callee="c", opening_line="Hi.", questions={"q": "q?"})
    call = manager.start_call("0255500000", brief)
    await asyncio.sleep(0.2)  # warming up, not dialled yet
    assert await manager.hang_up(call.id)
    await asyncio.wait_for(call.done.wait(), 10)
    assert dialled == [] and call.outcome == "hung_up_by_owner"


def test_pages_carry_the_credit_and_source_link():
    """The NOTICE's additional term (AGPL 7(b)): the credit stays in every page's footer."""
    from phone_agent import server

    html = server.page("Calls", "<p>x</p>")
    assert "AI on the Line</a> by Darek Margas" in html and "source</a> (AGPL-3.0)" in html
    assert "Jenny (Dioco)" in html


def test_downloaded_recording_carries_the_credit():
    import io
    import wave

    from phone_agent.credit import credit_line, with_credit

    src = io.BytesIO()
    with wave.open(src, "wb") as w:  # one second of 8 kHz phone audio, as MixMonitor writes it
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(8000); w.writeframes(b"\x10\x00" * 8000)
    outro = b"\x20\x00" * 8000  # half a second of "speech" at 16 kHz
    out = with_credit(src.getvalue(), credit_line("Jenny (Dioco)"), outro, 16000)
    with wave.open(io.BytesIO(out)) as w:
        assert w.getframerate() == 8000
        assert w.getnframes() == 8000 + int(8000 * 0.6) + 4000  # the call, a pause, the spoken credit
    assert b"Made with AI on the Line (https://github.com/darek-margas/ai-on-the-line). Voice: Jenny (Dioco)" in out
    assert b"ICMT" in out and b"ISFT" in out
    # Without the spoken part only the metadata changes.
    with wave.open(io.BytesIO(with_credit(src.getvalue(), credit_line()))) as w:
        assert w.getnframes() == 8000


def test_dialog_streams_say_from_ollama():
    """Ollama's NDJSON stream goes through the same sentence-by-sentence path."""
    import json as _json

    import httpx

    from phone_agent.dialog import Dialog

    reply = '{"say":"Sure. Which day suits you?","dtmf":"","answers":[],"notes":"","end_call":false,"outcome":"in_progress"}'
    sent = {}

    def handler(request):
        sent.update(_json.loads(request.content))
        lines = [_json.dumps({"message": {"role": "assistant", "content": reply[i : i + 5]}, "done": False})
                 for i in range(0, len(reply), 5)]
        lines.append(_json.dumps({"message": {"role": "assistant", "content": ""}, "done": True}))
        return httpx.Response(200, content="\n".join(lines).encode())

    dialog = Dialog.__new__(Dialog)
    dialog.owner, dialog.provider, dialog.model = "Alex", "ollama", "llama"
    dialog.ollama_url, dialog.keep_alive = "http://ollama", -1
    dialog._http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    heard = []

    async def on_sentence(sentence):
        heard.append(sentence)

    brief = Brief(goal="g", callee="c", opening_line="Hi.", questions={"q": "q?"})
    turn = asyncio.run(dialog.next_turn(brief, [], {}, "event", on_sentence=on_sentence))
    assert sent["stream"] is True and sent["think"] is False
    assert heard == ["Sure.", "Which day suits you?"]
    assert turn.spoken and turn.say == "Sure. Which day suits you?"


def test_clean_notes_and_goodbye():
    from phone_agent.dialog import clean_notes, says_goodbye

    for junk in ("in_progress", "In progress.", "completed", "none", "N/A", "unknown", ""):
        assert clean_notes(junk) == "", junk
    assert clean_notes(" Callee offered to book the room. ") == "Callee offered to book the room."
    assert says_goodbye("Thanks so much, Sarah. Bye now!") and says_goodbye("Have a good one.")
    assert not says_goodbye("No thanks, I'm just checking availability for Darek.")


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome, added", [("completed", True), ("voicemail", False)])
async def test_goodbye_added_when_model_ends_without_one(tmp_path, outcome, added):
    cfg = make_config(tmp_path)
    db = Database(cfg.data_dir)
    speech = ScriptedSpeech(["Hello, Riverside Motel."])
    dialog = ScriptedDialog([Turn(say="No thanks, I'm just checking.", end_call=True, outcome=outcome)])
    manager = CallManager(cfg, db, speech, dialog, None)
    server = await asyncio.start_server(manager.handle_audiosocket, "127.0.0.1", 0)
    manager.ari = BurstAri(server.sockets[0].getsockname()[1], [25], gaps=[75])
    import phone_agent.vad as vad
    orig = vad.webrtcvad.Vad.is_speech
    vad.webrtcvad.Vad.is_speech = lambda self, frame, rate: max(frame) > 8
    try:
        brief = Brief(goal="Ask", callee="Motel", opening_line="Hi, I'm Alex's assistant. Is a room free?",
                      questions={"free": "room free"})
        call = manager.start_call("0255500000", brief)
        await asyncio.wait_for(call.done.wait(), 60)
    finally:
        vad.webrtcvad.Vad.is_speech = orig
        server.close()
    agent = [text for who, text in db.get_call(call.id)["transcript"] if who == "agent"]
    assert (agent[-1] == "Thanks, bye!") == added, agent
