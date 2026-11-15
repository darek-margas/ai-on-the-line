# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Darek Margas. See NOTICE.
"""TokenAuth in front of a stand-in app (no PBX, no MCP)."""

from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from phone_agent.auth import COOKIE, TokenAuth

TOKEN = "s3cret-token"


def client() -> TestClient:
    inner = Starlette(routes=[Route("/{path:path}", lambda r: PlainTextResponse("ok"), methods=["GET", "POST"])])
    return TestClient(TokenAuth(inner, TOKEN), follow_redirects=False)


def test_rejects_missing_and_wrong_tokens():
    c = client()
    assert c.post("/mcp").status_code == 401
    assert c.post("/mcp", headers={"Authorization": "Bearer nope"}).status_code == 401
    assert c.post("/mcp").headers["www-authenticate"] == "Bearer"


def test_accepts_bearer_token():
    r = client().post("/mcp", headers={"Authorization": f"Bearer {TOKEN}"})
    assert (r.status_code, r.text) == (200, "ok")


def test_browser_is_sent_to_login_and_back():
    c = client()
    r = c.get("/calls/abc", headers={"Accept": "text/html"})
    assert r.status_code == 303 and r.headers["location"] == "/login?next=/calls/abc"
    assert c.get("/login?next=/calls/abc").status_code == 200

    wrong = c.post("/login?next=/calls/abc", data={"token": "nope"})
    assert wrong.status_code == 401 and COOKIE not in wrong.cookies

    r = c.post("/login?next=/calls/abc", data={"token": TOKEN})
    assert r.status_code == 303 and r.headers["location"] == "/calls/abc"
    assert "HttpOnly" in r.headers["set-cookie"]
    assert c.get("/calls/abc").text == "ok"  # the cookie now works


def test_login_never_redirects_off_site():
    r = client().post("/login?next=//evil.example/x", data={"token": TOKEN})
    assert r.headers["location"] == "/"
