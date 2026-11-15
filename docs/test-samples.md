# Test samples

Most of what goes wrong on a call is timing, not words. Someone says "Ah." and carries on, a voice robot pauses for a second mid-sentence, a receptionist answers two questions in two bursts. The transcript of such a call reads fine. You only find the problem by listening to the recording with the agent's timings next to it.

So the useful thing to collect is the other side's audio on its own, with a transcript that says roughly when each line was said. That can be played back into the agent in a test, and the test can check when the agent spoke.

A sample is a zip:

| File | What it is |
| --- | --- |
| `other.wav` | The other side only, as the PBX received it (agent calls, with the recordings mounted) |
| `agent.wav` | The agent only |
| `call.wav` | Both sides mixed |
| `recording.wav` | A recording made outside the agent, as recorded |
| `transcript.json` | Lines with `t` (seconds from the start), `who` and `text` |
| `transcript.txt` | The same, for reading |
| `README.txt` | What the files are |

Only the files that exist go in. Phone numbers and the brief aren't included.

## From a call the agent made or answered

Open the call page and use the **Test sample** link under the transcript. If `RECORDINGS_DIR` points at the PBX's recordings, the zip has each side on its own. Without it you get the transcript and timings only; the audio then has to come from the PBX (see below, or copy `phone-agent/<call id>-rx.wav` from `/var/spool/asterisk/monitor`).

The same from the command line:

```sh
docker compose exec phone-agent python -m phone_agent.sample --call 3f2a91c0 -o /data/sample.zip
```

The call id can be shortened to its first characters. `/data` is the container's data folder, so the file ends up in `./data` next to your `docker-compose.yml`.

Calls recorded before 2026.11.1.1 have no timings; their lines show `?` for the time.

## From any FreePBX recording

Calls you took yourself are often the best material: a salesperson who doesn't stop, a support line with a long menu. FreePBX keeps them under `/var/spool/asterisk/monitor/YYYY/MM/DD/`.

With that folder mounted as `RECORDINGS_DIR`:

```sh
docker compose exec phone-agent python -m phone_agent.sample \
    --recording 2026/11/14/external-0255501234-0412000000-20261114-101501-1763082901.12.wav \
    --who caller,me -o /data/sample.zip
```

Or copy the file into `./data` first and pass `/data/<file>.wav`. The tool finds stretches of speech, transcribes each one with your Whisper server, and writes the same zip with the original file as `recording.wav`. A stereo recording is taken as one side per channel, and `--who` names them; a mono one has a single `line` speaker. Only 16-bit WAV is read: convert anything else with `sox in.gsm -b 16 out.wav` first.

## Before you share one

The text is redacted: numbers, email addresses, long references (`BBA7Q2K9`, or a reference read out letter by letter) and the owner's name become `<number>`, `<email>`, `<ref>` and `<owner>`. Read the transcript anyway; a name or address said in words won't be caught.

The audio isn't redacted, and can't be automatically. Listen to the whole thing. Share only calls where the other person agreed to be recorded, or ones you're otherwise free to publish, such as your own voice playing a caller against your own screener. Cut or bleep anything private with Audacity or `sox` before sharing.

To contribute a sample, open a [Test sample issue](https://github.com/darek-margas/ai-on-the-line/issues/new?template=test_sample.yml) and attach the zip. Samples added to the project are shared under CC BY-SA 4.0, like the rest of `docs/`.

---

Part of [AI on the Line](https://github.com/darek-margas/ai-on-the-line) by Darek Margas. This page is licensed under [CC BY-SA 4.0](LICENSE.md).
