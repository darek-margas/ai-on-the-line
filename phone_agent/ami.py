"""Asterisk Manager Interface over HTTP, only for pressing keys.

Phone menus often ignore in-band tones and want real DTMF (RFC 2833), which
ARI won't send on channels outside a Stasis app and AudioSocket won't take
from us. AMI's PlayDTMF works on any channel.
"""

import asyncio

import httpx

DIGIT_MS = 250
GAP_SECONDS = 0.15


class Ami:
    def __init__(self, url: str, user: str, password: str):
        self.url = url
        self.user = user
        self.password = password
        self.http = httpx.AsyncClient(timeout=10)  # keeps the mansession_id cookie
        self._logged_in = False

    async def _post(self, **fields: str) -> str:
        response = await self.http.post(self.url, data=fields)
        response.raise_for_status()
        return response.text

    async def _login(self) -> None:
        text = await self._post(action="Login", username=self.user, secret=self.password)
        if "Response: Success" not in text:
            raise RuntimeError(f"AMI login failed: {text.strip()[:200]}")
        self._logged_in = True

    async def _action(self, **fields: str) -> None:
        for attempt in range(2):
            if not self._logged_in:
                await self._login()
            text = await self._post(**fields)
            if "Response: Success" in text:
                return
            if attempt == 0 and ("Authentication" in text or "not logged" in text.lower() or "Permission" in text):
                self._logged_in = False  # session expired: log in again once
                continue
            raise RuntimeError(f"AMI {fields.get('action')} failed: {text.strip()[:200]}")

    async def play_dtmf(self, channel: str, digits: str) -> None:
        for digit in digits:
            await self._action(action="PlayDTMF", channel=channel, digit=digit, duration=str(DIGIT_MS))
            await asyncio.sleep(DIGIT_MS / 1000 + GAP_SECONDS)
