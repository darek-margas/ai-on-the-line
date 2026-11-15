# Test samples

Most of what goes wrong on a call is timing, not words. Someone says "Ah." and carries on, a voice robot pauses for a second mid-sentence, a receptionist answers two questions in two bursts. The transcript of such a call reads fine. You only find the problem by listening to the recording with the agent's timings next to it.

So the useful thing to collect is the other side's audio on its own, with a transcript that says roughly when each line was said. That can be played back into the agent in a test, and the test can check when the agent spoke.

## Re-voiced or original

The trouble is that the interesting calls have someone else's voice on them: a receptionist, a salesperson, a support agent. They didn't agree to be recorded for anyone's project, let alone published.

So samples come in two versions:

- **Re-voiced.** Every stretch of speech is replaced by the agent's TTS voice reading the redacted transcript of it. It starts at the same moment, lasts exactly as long and is as loud. Stretches where nothing was recognised (a cough, line noise, a bit of hold music) become noise of the same loudness. The pauses between and around them stay exactly where they were. Nobody's voice is left, only the timing. This is the version to share.
- **Original.** The audio as the PBX recorded it. Keep it to yourself unless everyone on it agreed.

The re-voiced version loses accents, mumbling and line quality, which matter to speech recognition. What it keeps (when speech starts and stops, how long the pauses are, a two-part answer arriving in two bursts) is what the turn-taking depends on.

## What's in a sample

A sample is a zip:

| File | What it is |
| --- | --- |
| `other.wav` | The other side only (agent calls, with the recordings mounted) |
| `agent.wav` | The agent only |
| `call.wav` | Both sides mixed |
| `recording.wav` | A recording made outside the agent |
| `transcript.json` | Lines with `t` (seconds from the start), `who` and `text`; `"revoiced": true` if it is |
| `transcript.txt` | The same, for reading |
| `README.txt` | What the files are, and which version this is |

Only the files that exist go in. Phone numbers and the brief aren't included.

## From a call the agent made or answered

The call page has two links under the transcript: **re-voiced** and **original audio**. Both need `RECORDINGS_DIR` pointing at the PBX's recordings (see [the FreePBX guide](asterisk-freepbx.md#12-recordings-in-the-container)). Without it there's a single link with the transcript and timings only.

Re-voicing sends every stretch of speech through Whisper and Piper, so it takes a few seconds: about 7 for a 46-second call on my setup. It's refused while a call is in progress, so it doesn't slow down a live call.

From the command line:

```sh
docker compose exec phone-agent python -m phone_agent.sample --call 3f2a91c0 --revoice -o /data/sample.zip
```

Leave out `--revoice` for the original audio. The call id can be shortened to its first characters. `/data` is the container's data folder, so the file ends up in `./data` next to your `docker-compose.yml`.

Calls recorded before 2026.11.1.1 have no timings in the transcript; their lines show `?`. The audio's timing is still exact.

## From any FreePBX recording

Calls you took yourself are often the best material: a salesperson who doesn't stop, a support line with a long menu. FreePBX keeps them under `/var/spool/asterisk/monitor/YYYY/MM/DD/`.

With that folder mounted as `RECORDINGS_DIR`:

```sh
docker compose exec phone-agent python -m phone_agent.sample --revoice \
    --recording 2026/11/14/external-0255501234-0412000000-20261114-101501-1763082901.12.wav \
    --who caller,me -o /data/sample.zip
```

Or copy the file into `./data` first and pass `/data/<file>.wav`. The tool finds stretches of speech and transcribes each one with your Whisper server. With `--revoice` it also re-voices them, and without it it copies the original. A stereo recording is taken as one side per channel, and `--who` names them. A mono recording has a single `line` speaker, so both people end up in the same TTS voice. Only 16-bit WAV is read: convert anything else with `sox in.gsm -b 16 out.wav` first.

## Before you share one

Read `transcript.txt`, even for a re-voiced sample: the voice is gone, but the words are still there, read out. Numbers, email addresses, long references (`BBA7Q2K9`, or one read out letter by letter) and the owner's name are replaced automatically. The re-voiced audio says "a number", "an email address", "a reference" and "Alex" in their place. A name, a street or a company said in plain words isn't caught. If one is there, cut that stretch with Audacity, or don't share the sample.

To contribute, open a [Test sample issue](https://github.com/darek-margas/ai-on-the-line/issues/new?template=test_sample.yml) and attach a **re-voiced** zip. Original audio is accepted only when every voice on it is yours, for example you playing a caller against your own screener. The project doesn't take original recordings of anyone else, publicly or privately. Samples added to the project are shared under CC BY-SA 4.0, like the rest of `docs/`.

---

Part of [AI on the Line](https://github.com/darek-margas/ai-on-the-line) by Darek Margas. This page is licensed under [CC BY-SA 4.0](LICENSE.md).
