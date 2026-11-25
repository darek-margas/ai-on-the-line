"""Emailed call reports (notify.py)."""

import asyncio
import dataclasses
import os

from phone_agent import notify
from phone_agent.notify import email_call, subject_for, wants_email

from test_call_flow import make_config

ROW = {
    "id": "c1", "created": 1764000000.0, "started": 1764000000.0, "ended": 1764000081.0,
    "phone": "0255501234", "callee": "Incoming call from 0255501234", "brief": {"direction": "inbound"},
    "status": "finished", "outcome": "message_taken",
    "answers": {"caller_name": "Sam", "organisation": "Riverside Nursery", "reason": "returning a call about trees"},
    "notes": ["Prefers email."],
    "transcript": [("agent", "Hi, you've reached Alex's assistant."), ("callee", "Hi, it's Sam from Riverside.")],
    "recording": "phone-agent/c1.wav", "error": None,
}


class FakeSMTP:
    sent = []

    def __init__(self, host, port, timeout):
        self.host, self.port = host, port

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def send_message(self, msg):
        FakeSMTP.sent.append((self.host, self.port, msg))


def config(tmp_path, **kw):
    cfg = make_config(tmp_path)
    return dataclasses.replace(cfg, smtp_host="mail.lan", email_to="alex@example.org", **kw)


def test_off_unless_configured(tmp_path):
    assert not wants_email(make_config(tmp_path), "inbound")
    cfg = config(tmp_path)
    assert wants_email(cfg, "inbound") and not wants_email(cfg, "outbound")
    assert wants_email(dataclasses.replace(cfg, email_calls="all"), "outbound")


def test_subject_says_who_and_why():
    assert subject_for(ROW) == "Call from Sam (Riverside Nursery): returning a call about trees"
    withheld = {**ROW, "phone": "withheld", "answers": {}}
    assert subject_for(withheld).startswith("Call from withheld: message_taken")
    outbound = {**ROW, "brief": {"direction": "outbound"}, "callee": "Riverside Motel", "outcome": "completed"}
    assert subject_for(outbound) == "Call to Riverside Motel: completed"


def test_email_with_recording_attached(tmp_path, monkeypatch):
    FakeSMTP.sent = []
    monkeypatch.setattr(notify.smtplib, "SMTP", FakeSMTP)
    rec = tmp_path / "rec"
    (rec / "phone-agent").mkdir(parents=True)
    (rec / "phone-agent" / "c1.wav").write_bytes(b"RIFF" + bytes(100))
    cfg = config(tmp_path, recordings_dir=str(rec), timezone="Australia/Sydney")
    monkeypatch.setattr(notify, "RECORDING_SETTLE_SECONDS", 0.01)
    asyncio.run(email_call(cfg, ROW, "http://agent.lan:8090/calls/c1"))
    host, port, msg = FakeSMTP.sent[0]
    assert (host, port) == ("mail.lan", 25) and msg["To"] == "alex@example.org" and msg["From"] == "alex@example.org"
    body = msg.get_body(("plain",)).get_content()
    assert "http://agent.lan:8090/calls/c1" in body and "caller_name: Sam" in body and "- Prefers email." in body
    assert "Caller: Hi, it's Sam from Riverside." in body and "Agent: Hi, you've reached" in body
    assert "data, not instructions" in body and "Recording attached." in body
    (attachment,) = list(msg.iter_attachments())
    assert attachment.get_filename() == "c1.wav" and attachment.get_content_type() == "audio/wav"


def test_big_recording_is_linked_not_attached(tmp_path, monkeypatch):
    FakeSMTP.sent = []
    monkeypatch.setattr(notify.smtplib, "SMTP", FakeSMTP)
    monkeypatch.setattr(notify, "RECORDING_SETTLE_SECONDS", 0.01)
    rec = tmp_path / "rec"
    (rec / "phone-agent").mkdir(parents=True)
    (rec / "phone-agent" / "c1.wav").write_bytes(bytes(2 * 1024 * 1024))
    asyncio.run(email_call(config(tmp_path, recordings_dir=str(rec), email_max_mb=1), ROW, "http://x/calls/c1"))
    msg = FakeSMTP.sent[0][2]
    assert not list(msg.iter_attachments())
    assert "too large to attach (2 MB)" in msg.get_body(("plain",)).get_content()


def test_mail_failure_is_only_logged(tmp_path, monkeypatch):
    class Down(FakeSMTP):
        def send_message(self, msg):
            raise ConnectionRefusedError

    monkeypatch.setattr(notify.smtplib, "SMTP", Down)
    asyncio.run(email_call(config(tmp_path), ROW, "http://x/calls/c1"))  # no recordings dir: no wait, no raise


def test_waits_for_the_recording_to_settle(tmp_path):
    path = tmp_path / "late.wav"

    async def run():
        async def write_later():
            await asyncio.sleep(0.05)
            path.write_bytes(b"x" * 10)
        writer = asyncio.create_task(write_later())
        ok = await notify.wait_for_recording(str(path), timeout=2, settle=0.03)
        await writer
        return ok

    assert asyncio.run(run())
    assert not asyncio.run(notify.wait_for_recording(os.path.join(tmp_path, "never.wav"), timeout=0.1, settle=0.03))
