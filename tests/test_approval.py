# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Darek Margas. See NOTICE.
"""Calls that wait for the owner's approval or for their time (CALL_APPROVAL, run_at)."""

import asyncio
import dataclasses
import time

import pytest

from phone_agent.auth import TokenAuth
from phone_agent.calls import CallManager
from phone_agent.db import Database
from phone_agent.dialog import Brief
from test_call_flow import FakeDialog, FakeSpeech, make_config

BRIEF = Brief(goal="g", callee="Motel", opening_line="Hi, I'm Alex's assistant.", questions={"q": "q?"})


class CountingAri:
    def __init__(self):
        self.dialled = []

    async def originate(self, endpoint, *args):
        self.dialled.append(endpoint)
        return "chan-1"

    async def channel_exists(self, channel_id):
        return False  # "no answer": the call ends straight away

    async def hangup(self, channel_id):
        pass


def manager_for(tmp_path, **changes) -> tuple[CallManager, CountingAri]:
    cfg = dataclasses.replace(make_config(tmp_path), **changes)
    ari = CountingAri()
    return CallManager(cfg, Database(cfg.data_dir), FakeSpeech(), FakeDialog(), ari), ari


@pytest.mark.asyncio
async def test_new_number_waits_for_approval_then_is_remembered(tmp_path):
    manager, ari = manager_for(tmp_path, call_approval="new_numbers", approval_token="a")
    call = manager.start_call("02 5550 0000", BRIEF)
    assert call.status == "awaiting_approval"
    await asyncio.sleep(0.2)
    assert ari.dialled == []
    assert manager.approve(call.id, remember=True)
    await asyncio.wait_for(call.done.wait(), 10)
    assert ari.dialled == ["Local/0255500000@from-internal/n"]
    # Same number again, written differently: approved before, so it dials at once.
    again = manager.start_call("+61 2 5550 0000", BRIEF)
    assert again.status == "queued" and again.schedule["approval_note"] == "number approved before"
    await asyncio.wait_for(again.done.wait(), 10)
    assert len(ari.dialled) == 2


@pytest.mark.asyncio
async def test_extensions_and_listed_numbers_skip_approval(tmp_path):
    manager, _ = manager_for(tmp_path, call_approval="new_numbers", approval_token="a",
                             approved_numbers=["+61 2 5550 1111"])
    assert manager.start_call("7999", BRIEF).status == "queued"
    assert manager.start_call("0255501111", BRIEF).status == "queued"
    assert manager.start_call("0255502222", BRIEF).status == "awaiting_approval"


@pytest.mark.asyncio
async def test_all_mode_asks_every_time(tmp_path):
    manager, _ = manager_for(tmp_path, call_approval="all", approval_token="a")
    assert manager.start_call("7999", BRIEF).status == "awaiting_approval"
    call = manager.start_call("0255500000", BRIEF)
    manager.approve(call.id, remember=True)  # "all" never remembers
    assert manager.start_call("0255500000", BRIEF).status == "awaiting_approval"


@pytest.mark.asyncio
async def test_rejected_and_cancelled_calls_never_dial(tmp_path):
    manager, ari = manager_for(tmp_path, call_approval="new_numbers", approval_token="a")
    rejected = manager.start_call("0255500000", BRIEF)
    cancelled = manager.start_call("0255500001", BRIEF)
    await asyncio.sleep(0.1)
    assert manager.reject(rejected.id)
    assert await manager.hang_up(cancelled.id)
    await asyncio.wait_for(rejected.done.wait(), 5)
    await asyncio.wait_for(cancelled.done.wait(), 5)
    assert ari.dialled == []
    assert (rejected.status, rejected.outcome) == ("cancelled", "rejected")
    assert (cancelled.status, cancelled.outcome) == ("cancelled", "hung_up_by_owner")
    assert not manager.approve(rejected.id)  # too late


@pytest.mark.asyncio
async def test_unapproved_call_expires(tmp_path):
    manager, ari = manager_for(tmp_path, call_approval="new_numbers", approval_token="a", approval_hours=0.3 / 3600)
    call = manager.start_call("0255500000", BRIEF)
    await asyncio.wait_for(call.done.wait(), 5)
    assert (call.status, call.outcome) == ("cancelled", "expired") and ari.dialled == []


@pytest.mark.asyncio
async def test_scheduled_call_waits_for_its_time(tmp_path):
    manager, ari = manager_for(tmp_path)
    call = manager.start_call("0255500000", BRIEF, run_at=time.time() + 0.6)
    assert call.status == "scheduled"
    await asyncio.sleep(0.3)
    assert ari.dialled == []
    await asyncio.wait_for(call.done.wait(), 10)
    assert len(ari.dialled) == 1


@pytest.mark.asyncio
async def test_waiting_calls_survive_a_restart(tmp_path):
    manager, _ = manager_for(tmp_path, call_approval="new_numbers", approval_token="a")
    call = manager.start_call("0255500000", BRIEF, run_at=time.time() + 3600)
    manager.db.fail_unfinished_calls()  # what a restart does first
    later, ari = manager_for(tmp_path, call_approval="new_numbers", approval_token="a")
    assert later.resume_pending() == 1
    resumed = later.calls[call.id]
    assert resumed.status == "awaiting_approval" and resumed.brief.callee == "Motel"
    assert later.approve(call.id)
    assert resumed.status == "scheduled"
    assert await later.hang_up(call.id)
    await asyncio.wait_for(resumed.done.wait(), 5)
    assert ari.dialled == [] and later.db.get_call(call.id)["status"] == "cancelled"


def test_calling_hours_apply_when_it_dials(tmp_path):
    manager, _ = manager_for(tmp_path, call_hours=("09:00", "17:00"))
    from datetime import datetime
    from zoneinfo import ZoneInfo
    night = datetime(2026, 11, 20, 23, 0, tzinfo=ZoneInfo("Australia/Sydney")).timestamp()
    noon = datetime(2026, 11, 20, 12, 0, tzinfo=ZoneInfo("Australia/Sydney")).timestamp()
    assert "calling hours are 09:00-17:00" in manager.hours_error("0255500000", "Australia/Sydney", False, night)
    assert manager.hours_error("0255500000", "Australia/Sydney", False, noon) is None
    assert manager.hours_error("7999", "Australia/Sydney", False, night) is None


def test_approval_settings(monkeypatch):
    for key in ("CALL_APPROVAL", "APPROVAL_TOKEN", "API_TOKEN", "APPROVED_NUMBERS"):
        monkeypatch.delenv(key, raising=False)
    from phone_agent.config import _approval

    assert _approval()["call_approval"] == "off"
    monkeypatch.setenv("APPROVAL_TOKEN", "secret")
    assert _approval()["call_approval"] == "new_numbers"  # a token turns it on
    monkeypatch.setenv("API_TOKEN", "secret")
    with pytest.raises(ValueError, match="must differ"):
        _approval()
    monkeypatch.delenv("APPROVAL_TOKEN")
    monkeypatch.setenv("CALL_APPROVAL", "all")
    with pytest.raises(ValueError, match="needs APPROVAL_TOKEN"):
        _approval()


def test_only_the_approval_token_approves(tmp_path):
    from starlette.testclient import TestClient

    from phone_agent.server import build_app

    manager, _ = manager_for(tmp_path, call_approval="new_numbers", approval_token="approve-me", api_token="api")

    async def place():
        return manager.start_call("0255500000", BRIEF)

    loop = asyncio.new_event_loop()
    call = loop.run_until_complete(place())
    mcp = build_app(manager.cfg, manager, manager.db, None, None, None)
    app = TokenAuth(mcp.streamable_http_app(), "api", "approve-me")
    client = TestClient(app)
    as_claude = {"Authorization": "Bearer api"}
    as_owner = {"Authorization": "Bearer approve-me"}

    page = client.get(f"/calls/{call.id}", headers=as_claude)
    assert "Sign in with the approval token" in page.text and "<button>Approve" not in page.text
    assert client.post(f"/calls/{call.id}/approve", headers=as_claude).status_code == 403
    assert call.status == "awaiting_approval"

    page = client.get(f"/calls/{call.id}", headers=as_owner)
    assert "<button>Approve" in page.text and "0255500000" in page.text.replace(" ", "")
    response = client.post(f"/calls/{call.id}/approve", headers=as_owner, data={"remember": "1"},
                           follow_redirects=False)
    assert response.status_code == 303 and call.status == "queued"
    assert manager.db.number_approved("0255500000")
    loop.close()


def test_approved_numbers_page(tmp_path):
    from starlette.testclient import TestClient

    from phone_agent.server import build_app

    manager, _ = manager_for(tmp_path, call_approval="new_numbers", approval_token="approve-me", api_token="api")
    manager.db.approve_number("0255500000", "")
    mcp = build_app(manager.cfg, manager, manager.db, None, None, None)
    client = TestClient(TokenAuth(mcp.streamable_http_app(), "api", "approve-me"), follow_redirects=False)
    as_claude = {"Authorization": "Bearer api"}
    as_owner = {"Authorization": "Bearer approve-me"}

    page = client.get("/approved", headers=as_claude)
    assert "0255500000" in page.text and "Remove" not in page.text
    assert client.post("/approved", headers=as_claude, data={"remove": "0255500000"}).status_code == 403
    assert client.post("/approved", headers=as_claude, data={"add": "0255501111"}).status_code == 403

    assert client.post("/approved", headers=as_owner, data={"add": "+61 2 5550 1111"}).status_code == 303
    assert manager.db.number_approved("0255501111")  # stored as it will be dialled
    assert client.post("/approved", headers=as_owner, data={"remove": "0255500000"}).status_code == 303
    assert not manager.db.number_approved("0255500000")
    assert "000" in client.post("/approved", headers=as_owner, data={"add": "000"}).text  # refused, with the reason


def test_deleting_an_ended_call(tmp_path):
    """Only the owner deletes, only a call that has ended, after a confirm()."""
    from starlette.testclient import TestClient

    from phone_agent.server import build_app

    manager, _ = manager_for(tmp_path, call_approval="new_numbers", approval_token="approve-me", api_token="api")
    async def place():
        return manager.start_call("0255500000", BRIEF)

    loop = asyncio.new_event_loop()
    call = loop.run_until_complete(place())
    mcp = build_app(manager.cfg, manager, manager.db, None, None, None)
    client = TestClient(TokenAuth(mcp.streamable_http_app(), "api", "approve-me"), follow_redirects=False)
    as_claude = {"Authorization": "Bearer api"}
    as_owner = {"Authorization": "Bearer approve-me"}

    assert "Delete call" not in client.get(f"/calls/{call.id}", headers=as_owner).text  # still waiting
    assert client.post(f"/calls/{call.id}/delete", headers=as_owner).status_code == 409
    manager.reject(call.id)
    assert "Delete call" not in client.get(f"/calls/{call.id}", headers=as_claude).text
    assert client.post(f"/calls/{call.id}/delete", headers=as_claude).status_code == 403
    page = client.get(f"/calls/{call.id}", headers=as_owner).text
    assert "Delete call" in page and "confirm(" in page
    assert client.post(f"/calls/{call.id}/delete", headers=as_owner).status_code == 303
    assert manager.get(call.id) is None
    assert client.get(f"/calls/{call.id}", headers=as_owner).status_code == 404
    loop.close()
