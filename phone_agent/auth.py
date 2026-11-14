"""Optional shared-token auth for everything on the HTTP port (MCP, status pages, recordings).

MCP clients send "Authorization: Bearer <token>". Browsers get a small login form that
sets a cookie, so the status-page links in tool results still open with one sign-in.
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
    def __init__(self, app, token: str):
        self.app = app
        self.token = token.encode()

    def _valid(self, value: str | None) -> bool:
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
        if self._valid(self._presented(headers)):
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
        if not target.startswith("/") or target.startswith("//"):
            target = "/"  # only ever send people back into this site
        error = ""
        if scope["method"] == "POST":
            body = b""
            while True:
                message = await receive()
                body += message.get("body", b"")
                if not message.get("more_body"):
                    break
            if self._valid(parse_qs(body.decode()).get("token", [""])[0].strip()):
                cookie = (f"{COOKIE}={self.token.decode()}; Path=/; HttpOnly; SameSite=Lax; "
                          f"Max-Age={COOKIE_DAYS * 86400}")
                if scope.get("scheme") == "https" or headers.get(b"x-forwarded-proto") == b"https":
                    cookie += "; Secure"
                return await _respond(send, 303, b"", [(b"location", target.encode()),
                                                       (b"set-cookie", cookie.encode())])
            error = "<p class='error'>Wrong token.</p>"
        from .server import page  # the shared page style; imported late to avoid a cycle
        form = (f"<h1>Phone agent</h1>{error}<form method='post' action='/login?next={html.escape(quote(target))}'>"
                "<label>Access token<input name='token' type='password' autofocus required></label>"
                "<button>Sign in</button></form>")
        await _respond(send, 401 if error else 200, page("Sign in", form).encode(),
                       [(b"content-type", b"text/html; charset=utf-8")])


async def _respond(send, status: int, body: bytes, headers: list[tuple[bytes, bytes]]):
    await send({"type": "http.response.start", "status": status,
                "headers": headers + [(b"content-length", str(len(body)).encode())]})
    await send({"type": "http.response.body", "body": body})
