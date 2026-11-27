# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Darek Margas. See NOTICE.
"""Optional shared-token auth for everything on the HTTP port (MCP, status pages, recordings).

MCP clients send "Authorization: Bearer <token>". Browsers get a small login form that
sets a cookie, so the status-page links in tool results still open with one sign-in.

A second secret, APPROVAL_TOKEN, opens everything API_TOKEN does and also lets
you approve calls. Requests made with it carry scope["state"]["approver"] = True.
"""

import hmac
import html
import logging
from http.cookies import SimpleCookie
from urllib.parse import parse_qs, quote

log = logging.getLogger(__name__)

COOKIE = "phone_agent_token"
COOKIE_DAYS = 90


class TokenAuth:
    def __init__(self, app, token: str, approval_token: str = ""):
        self.app = app
        self.token = token.encode()
        self.approval_token = approval_token.encode()

    def _approver(self, value: str | None) -> bool:
        return bool(value and self.approval_token) and hmac.compare_digest(value.encode(), self.approval_token)

    def _valid(self, value: str | None) -> bool:
        if self._approver(value):
            return True
        if not self.token:  # ALLOW_NO_AUTH: viewing is open, approving still needs the token
            return True
        return bool(value) and hmac.compare_digest(value.encode(), self.token)

    def _presented(self, headers: dict[bytes, bytes]) -> str | None:
        scheme, _, value = headers.get(b"authorization", b"").decode(errors="replace").partition(" ")
        if scheme.lower() == "bearer" and value:
            return value.strip()
        cookie = SimpleCookie(headers.get(b"cookie", b"").decode(errors="replace"))
        return cookie[COOKIE].value if COOKIE in cookie else None

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        headers = dict(scope["headers"])
        path = scope["path"]
        if path == "/login":
            return await self._login(scope, receive, send, headers)
        if path == "/logout":  # forget the cookie, e.g. to switch between the two tokens
            return await _respond(send, 303, b"", [(b"location", b"/login"),
                                                   (b"set-cookie", f"{COOKIE}=; Path=/; HttpOnly; Max-Age=0".encode())])
        presented = self._presented(headers)
        if self._valid(presented):
            scope.setdefault("state", {})["approver"] = self._approver(presented)
            return await self.app(scope, receive, send)
        if path == "/inbound" and scope["method"] == "POST":
            # The dialplan may send the token as a form field instead of a header
            # (a header set in the dialplan ends up in Asterisk's log). The route
            # reads the body and checks it with this.
            scope.setdefault("state", {}).update(approver=False, token_check=self._valid)
            return await self.app(scope, receive, send)
        if scope["method"] == "GET" and b"text/html" in headers.get(b"accept", b""):
            target = path + ("?" + scope["query_string"].decode() if scope["query_string"] else "")
            return await _respond(send, 303, b"", [(b"location", f"/login?next={quote(target)}".encode())])
        log.warning("Refused %s %s: %s", scope["method"], path,
                    "wrong token" if self._presented(headers) else "no token sent")
        await _respond(send, 401, b'{"error":"missing or wrong token"}',
                       [(b"content-type", b"application/json"), (b"www-authenticate", b"Bearer")])

    async def _login(self, scope, receive, send, headers):
        query = parse_qs(scope["query_string"].decode())
        target = query.get("next", ["/"])[0]
        if (not target.startswith("/") or target.startswith("//") or "\\" in target
                or any(ord(ch) < 32 for ch in target)):
            # Only ever send people back into this site: browsers read "/\\host" as "//host".
            target = "/"
        error = ""
        if scope["method"] == "POST":
            body = b""
            while True:
                message = await receive()
                body += message.get("body", b"")
                if not message.get("more_body"):
                    break
            entered = parse_qs(body.decode()).get("token", [""])[0].strip()
            if entered and self._valid(entered):
                cookie = (f"{COOKIE}={entered}; Path=/; HttpOnly; SameSite=Lax; "
                          f"Max-Age={COOKIE_DAYS * 86400}")
                if scope.get("scheme") == "https" or headers.get(b"x-forwarded-proto") == b"https":
                    cookie += "; Secure"
                return await _respond(send, 303, b"", [(b"location", target.encode()),
                                                       (b"set-cookie", cookie.encode())])
            error = "<p class='error'>Wrong token.</p>"
        from .server import page  # the shared page style; imported late to avoid a cycle
        form = (f"<h1>Phone agent</h1>{error}<form method='post' action='/login?next={html.escape(quote(target))}'>"
                "<label>Access or approval token<input name='token' type='password' autofocus required></label>"
                "<button>Sign in</button></form>")
        await _respond(send, 401 if error else 200, page("Sign in", form).encode(),
                       [(b"content-type", b"text/html; charset=utf-8")])


async def _respond(send, status: int, body: bytes, headers: list[tuple[bytes, bytes]]):
    await send({"type": "http.response.start", "status": status,
                "headers": headers + [(b"content-length", str(len(body)).encode())]})
    await send({"type": "http.response.body", "body": body})
