"""Places a test call to the scripted receptionist (extension 7999) through the MCP API
and prints the answers and transcript. Run inside the container:

    docker exec -i phone-agent python - < tools/try_call.py
"""

import asyncio
import json
import time

import httpx

U = "http://127.0.0.1:8090/mcp"
H = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}


def parse(r):
    msg = [json.loads(l[5:]) for l in r.text.splitlines() if l.startswith("data:")][-1]
    return json.loads(msg["result"]["content"][0]["text"])


async def main():
    async with httpx.AsyncClient(timeout=300) as c:
        r = await c.post(U, headers=H, json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}}})
        h = {**H, "mcp-session-id": r.headers["mcp-session-id"]}
        await c.post(U, headers=h, json={"jsonrpc": "2.0", "method": "notifications/initialized"})
        ids = iter(range(2, 1000))

        async def call(name, args):
            return parse(await c.post(U, headers=h, json={"jsonrpc": "2.0", "id": next(ids), "method": "tools/call",
                                                          "params": {"name": name, "arguments": args}}))

        d = await call("check_setup", {})
        print("check_setup:", {k: d[k] for k in ("ari", "asterisk", "dialog_llm", "stt_whisper", "tts_piper") if k in d})
        d = await call("place_call", {
            "phone": "7999",
            "callee": "Riverside Motel reception (test)",
            "goal": "Find out if they have a room for 5 people for 2 nights, 12 to 14 December, and the price.",
            "opening_line": "Hi, this is an automated assistant calling on behalf of Alex. I'm asking about room "
                            "availability for five people, for the twelfth to the fourteenth of December.",
            "questions": [
                {"name": "availability", "description": "Is there a room or rooms for 5 people, 12 to 14 December?"},
                {"name": "room_setup", "description": "Bed configuration of the room(s) offered"},
                {"name": "price", "description": "Price per night, and whether that is for the whole room"},
                {"name": "breakfast", "description": "Is breakfast included?"},
            ],
            "may_share": ["dates 12 to 14 December", "party of 5 (2 adults, 3 children)", "Alex will call back to book"],
            "ignore_calling_hours": True,
        })
        print("place_call:", d)
        cid = d["call_id"]
        t0 = time.time()
        while True:
            d = await call("get_call", {"call_id": cid, "wait_seconds": 50})
            print(f"[{time.time() - t0:.0f}s] status={d['status']} outcome={d['outcome']}")
            if d["status"] in ("finished", "not_answered", "failed"):
                break
        print(json.dumps({k: d[k] for k in ("answers", "notes", "error", "duration_seconds", "page")}, indent=1))
        print("\n".join(d["transcript"]))


asyncio.run(main())
