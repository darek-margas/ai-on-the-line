# Running the agent

This is the container side: the image, the settings, speech, ports, connecting Claude, updating and backups. The Asterisk side is in [asterisk-freepbx.md](asterisk-freepbx.md).

## The image

CI builds `ghcr.io/darek-margas/ai-on-the-line` for every push to `main` (`latest`) and for every release. Releases are tagged `yyyy.mm.sequence.subsequence`, e.g. `2026.11.1.2`. Pin a release tag in `docker-compose.yml` if you don't want calls to change behaviour after a pull:

```yaml
    image: ghcr.io/darek-margas/ai-on-the-line:2026.11.1.2
```

To build from your own checkout instead, uncomment `build: .` in the compose file and run `docker compose up -d --build`. The container runs as uid 10001 and writes only to `./data`.

## Settings

Everything is in `.env`; [.env.example](../.env.example) lists every setting with a comment. The ones that matter first:

- `OWNER_NAME`, `OWNER_PRONOUNS`: who the agent speaks for. It always introduces itself as that person's assistant.
- `SCREENER_RECORDING_NOTICE`: on by default. The screener's greeting tells callers the call is recorded, which many places require.
- `OWNER_BIO`: optional. What the call screener may say about you. Leave it empty and it won't make small talk about you.
- `TIMEZONE` and `CALL_HOURS`: outside numbers are only called within these hours, in this timezone. A single call can override it; extensions are always allowed.
- `ARI_*`, `AMI_*`, `AUDIOSOCKET_ADVERTISE`, `DIAL_ENDPOINT`: the Asterisk connection, see the FreePBX guide.
- `DIAL_*`: which numbers may be dialled, see [dialling.md](dialling.md).
- `API_TOKEN`: required. `openssl rand -hex 32` makes one.

Secrets can go in `secrets.env` next to `.env` (`API_TOKEN`, `ANTHROPIC_API_KEY`, `ARI_PASSWORD`). The compose file reads it if it exists, so `.env` can be shared or kept in git without them.

## Speech

Whisper (speech to text) and Piper (text to speech) are separate containers speaking the Wyoming protocol, the same ones Home Assistant uses.

My setup splits them by what they need. The agent and its own Piper run together on a small VM: Piper on a CPU renders a sentence in 100 to 400 ms, which is fast enough. Whisper is the expensive half, so it runs on a NAS with an NVIDIA GPU (`linuxserver/faster-whisper:gpu`, CUDA). That same Whisper serves Home Assistant's voice assistant, and the GPU is shared with Frigate too. The agent only needs `WYOMING_STT` pointing at it. A separate Whisper just for calls would sit idle most of the day while holding a few GB of GPU memory.

Whisper speed decides how fast the agent can answer, and on a phone line its accuracy is tested hardest by short answers: "Eight.", "Yes, it does.", a price. There's little context, and the 8 kHz line cuts off the top half of the voice. I compared models with [`tools/stt_bench.py`](../tools/stt_bench.py). It renders short answers with Piper, squeezes them through phone bandwidth and counts what Whisper gets exactly right. `small`, `medium.en` and `large-v3-turbo` came out about even. `distil-large-v3` was clearly worst on single words, even though it's often recommended as the fast option. I kept `large-v3-turbo`, which on the GPU takes about 300 to 400 ms per line. A run today got 9 of the 11 answers right at a median of 303 ms. The two misses were single digits, "Eight" heard as "Ace" and "Two" as "Choo", which is why numbers that matter are worth having read back on a call.

On a CPU a smaller model (`small.en`, `base.en`) keeps up, but expect more misheard names, numbers and one-word answers. Run the benchmark against your own Whisper before deciding:

```bash
docker compose exec -T phone-agent python - < tools/stt_bench.py
```

`check_setup` also times a round trip, so try it before calling anyone.

The compose file brings its own Piper with the Jenny voice, pinned to one image version, so calls don't start sounding different after an update. It's slowed down a little (`PIPER_LENGTH_SCALE=1.12`), which is easier to follow on a phone line. You can point `WYOMING_TTS` at Home Assistant's Piper instead, but then the agent speaks at whatever speed Home Assistant uses. With a different voice, set `CREDIT_VOICE` to it, or leave it empty.

Names Piper says wrongly can be respelled: `TTS_PRONOUNCE=Siobhan=Shivawn,Nguyen=Win`.

## The model

`DIALOG_LLM=claude:<model>` needs `ANTHROPIC_API_KEY`. Haiku is the default and is quick. I run Sonnet with its thinking set to answer straight away, which follows the conversation better for about the same delay.

`DIALOG_LLM=ollama:<model>` uses a local model through `OLLAMA_URL`. Replies stream either way. A smaller model is fast enough but follows the conversation less well. With gemma4 e4b I saw it ask a question that had just been answered.

The model has to be in memory before a call. Loading one from disk takes from several seconds to minutes, and a caller won't wait that long. So:

- The agent asks Ollama which models are loaded (`/api/ps`) when the container starts, and loads its model if it isn't there. If Ollama isn't reachable yet, it tries again every minute.
- Every request sends `OLLAMA_KEEP_ALIVE`, `-1` by default, which keeps the model loaded indefinitely. A shorter value lets Ollama unload it between calls, and the next call starts with a long silence.
- If the same Ollama serves something else, Home Assistant for example, use the model it already runs. Two models kept loaded forever need memory for both. If they don't fit, Ollama swaps them, and whichever is used next waits for the load.

`check_setup` loads the model too and reports how long that took.

Claude, on your side of MCP, only writes the brief and reads the result. The model above does the talking.

## Ports

| Port | Who connects | What for |
| --- | --- | --- |
| 8090 | MCP clients, your browser, the PBX (`/inbound`) | MCP, call pages, recordings, incoming-call registration |
| 9092 | The PBX | AudioSocket: call audio both ways |
| 10200 | The agent | Its own Piper (inside the compose network) |
| 10300 | The agent | Whisper, wherever you run it |

The agent connects out to Asterisk on 8088 (ARI and AMI over HTTP) and to Ollama on 11434 if you use it. Everything on 8090 needs the API token. Browsers are asked for it once and get a cookie. Don't expose 8090 or 9092 to the internet.

## Connecting Claude

Claude Code:

```bash
claude mcp add --transport http ai-on-the-line http://<docker-host>:8090/mcp --header "Authorization: Bearer <API_TOKEN>"
```

The Code tab in Claude Desktop uses the same setup. Claude Desktop's chat connectors expect OAuth rather than a fixed token, so for chat, bridge it with `mcp-remote` in `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "ai-on-the-line": {
      "command": "npx",
      "args": ["mcp-remote", "http://<docker-host>:8090/mcp", "--allow-http",
               "--header", "Authorization: Bearer <API_TOKEN>"]
    }
  }
}
```

Then ask it to run `check_setup`, and to call the test receptionist on 7999.

The tools are `place_call`, `get_call`, `list_calls`, `hang_up` and `check_setup`. Calls take minutes, so Claude places a call and then polls `get_call`. The MCP server's own instructions tell it how to write a brief and report back, so a plain request like "ring these three motels and ask about a family room on the 12th" works.

## Updating

```bash
docker compose pull && docker compose up -d
```

The call database gets new columns on start when a release needs them. Nothing has to be migrated by hand. Read the release notes for changed settings: 2026.11.1.1, for example, needed `DIAL_LOCAL_AREA_CODE` for 8-digit local numbers.

A call in progress is cut off by a restart and marked failed. Check `list_calls` first.

## Backups and personal data

`./data/phone-agent.db` (SQLite) holds every call: numbers, briefs, answers and full transcripts. That's personal data about the people you called and the people who called you. Back it up like any other database, and delete what you don't need. Recordings stay on the PBX.

Logs at `LOG_LEVEL=INFO` have timings only. `DEBUG` also logs what was said on each call, which helps when tuning turn-taking but puts transcripts in your log collector.

## Limits

- `MAX_CONCURRENT_CALLS` (default 1): outgoing calls run one at a time, extra ones queue.
- `MAX_INBOUND_CALLS` (default 1): one screened call at a time. Further callers hear the PBX's fallback.
- `MAX_CALL_SECONDS` and `INBOUND_MAX_SECONDS`: hard limits per call.
- `HOLD_TIMEOUT_SECONDS`: how long it waits on hold before giving up.

---

Part of [AI on the Line](https://github.com/darek-margas/ai-on-the-line) by Darek Margas. This page is licensed under [CC BY-SA 4.0](LICENSE.md).
