# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Darek Margas. See NOTICE.
"""Minimal Asterisk REST Interface client: originate, keypad tones, hang up."""

import httpx


class Ari:
    def __init__(self, url: str, user: str, password: str):
        self.http = httpx.AsyncClient(base_url=url, auth=(user, password), timeout=10)

    async def originate(self, endpoint: str, call_id: str, caller_id: str, audiosocket: str, timeout: int,
                        channel_id: str | None = None) -> str:
        """Dial out; when answered, the channel runs [phone-agent] which connects AudioSocket.
        channel_id: the id the channel gets, chosen by us so a lost reply can't
        leave a call ringing that we have no name for."""
        params = {
            "endpoint": endpoint,
            "context": "phone-agent",
            "extension": "s",
            "priority": 1,
            "timeout": timeout,
        }
        if caller_id:
            params["callerId"] = caller_id
        if channel_id:
            params["channelId"] = channel_id
        response = await self.http.post(
            "/channels",
            params=params,
            json={"variables": {"AGENT_UUID": call_id, "AGENT_SOCKET": audiosocket}},
        )
        response.raise_for_status()
        return response.json()["id"]

    async def channel_exists(self, channel_id: str) -> bool:
        response = await self.http.get(f"/channels/{channel_id}")
        if response.status_code == 404:
            return False
        response.raise_for_status()
        return True

    async def channel_name(self, channel_id: str) -> str:
        response = await self.http.get(f"/channels/{channel_id}")
        response.raise_for_status()
        return response.json()["name"]

    async def hangup(self, channel_id: str) -> None:
        response = await self.http.delete(f"/channels/{channel_id}")
        if response.status_code not in (204, 404):
            response.raise_for_status()

    async def info(self) -> dict:
        response = await self.http.get("/asterisk/info")
        response.raise_for_status()
        return response.json()
