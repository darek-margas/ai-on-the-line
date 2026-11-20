# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Darek Margas. See NOTICE.
"""The dialog model fails, hangs or is out of credit: the other side hears a
pre-rendered apology, never dead air or an error message."""

import asyncio

import pytest

from phone_agent.calls import CallManager
from phone_agent.db import Database
from phone_agent.dialog import Brief
from phone_agent.session import unavailable_line

from test_call_flow import FakeAri, FakeDialog, FakeSpeech, make_config
from test_inbound import StuckDialog, _inbound_with


class BrokenDialog(FakeDialog):
    """Claude out of credit: every request fails at once."""

    def __init__(self, warm_up_works=True):
        super().__init__()
        self.warm_up_works = warm_up_works

    async def warm_up(self, brief=None):
        if not self.warm_up_works:
            raise RuntimeError("Your credit balance is too low to access the Anthropic API.")

    async def next_turn(self, *args, **kwargs):
        self.calls += 1
        raise RuntimeError("Your credit balance is too low to access the Anthropic API.")


class CountingAri(FakeAri):
    def __init__(self, port):
        super().__init__(port)
        self.dialled = 0

    async def originate(self, *args, **kwargs):
        self.dialled += 1
        return await super().originate(*args, **kwargs)


async def outbound_with(tmp_path, dialog):
    cfg = make_config(tmp_path)
    db = Database(cfg.data_dir)
    manager = CallManager(cfg, db, FakeSpeech(), dialog, None)
    server = await asyncio.start_server(manager.handle_audiosocket, "127.0.0.1", 0)
    ari = manager.ari = CountingAri(server.sockets[0].getsockname()[1])
    import phone_agent.vad as vad
    orig = vad.webrtcvad.Vad.is_speech
    vad.webrtcvad.Vad.is_speech = lambda self, frame, rate: max(frame) > 8
    try:
        brief = Brief(goal="Ask price", callee="Hotel", opening_line="Hi, I'm Darek's assistant.",
                      questions={"price": "price per night"})
        call = manager.start_call("0285550000", brief)
        await asyncio.wait_for(call.done.wait(), 30)
        if ari.task:
            await asyncio.wait_for(ari.task, 10)
    finally:
        vad.webrtcvad.Vad.is_speech = orig
        server.close()
    return db.get_call(call.id), ari


@pytest.mark.asyncio
async def test_failing_model_ends_with_the_apology(tmp_path):
    dialog = BrokenDialog()
    saved, _ = await outbound_with(tmp_path, dialog)
    agent = [text for who, text in saved["transcript"] if who == "agent"]
    assert agent[-1] == unavailable_line(make_config(tmp_path).owner_name, inbound=False)
    assert "credit balance" not in " ".join(agent)
    assert saved["outcome"] == "technical_failure"
    assert "credit balance" in saved["error"]
    assert dialog.calls == 3  # the first question, its retry, then no more attempts


@pytest.mark.asyncio
async def test_hung_model_times_out_into_the_apology(tmp_path, monkeypatch):
    import phone_agent.session as session

    monkeypatch.setattr(session, "TURN_TIMEOUTS", (0.3, 0.3))
    dialog = StuckDialog()
    saved, _ = await outbound_with(tmp_path, dialog)
    agent = [text for who, text in saved["transcript"] if who == "agent"]
    assert agent[-1] == unavailable_line(make_config(tmp_path).owner_name, inbound=False)
    assert saved["outcome"] == "technical_failure"
    assert "no reply within" in saved["error"]
    assert dialog.calls == 3


@pytest.mark.asyncio
async def test_no_model_no_dialling(tmp_path):
    saved, ari = await outbound_with(tmp_path, BrokenDialog(warm_up_works=False))
    assert ari.dialled == 0
    assert saved["status"] == "failed"
    assert "not dialled" in saved["error"]


@pytest.mark.asyncio
async def test_inbound_without_model_greets_then_apologises(tmp_path):
    dialog = BrokenDialog(warm_up_works=False)
    call, _ = await _inbound_with(tmp_path, dialog)
    agent = [text for who, text in call.transcript if who == "agent"]
    assert agent == [call.brief.opening_line, unavailable_line(make_config(tmp_path).owner_name, inbound=True)]
    assert dialog.calls == 0  # known to be down: no attempts, no waiting
    assert call.outcome == "technical_failure"
