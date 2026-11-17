# Test samples

Most of what goes wrong on a call is timing, not words. Someone says "Ah." and carries on, a voice robot pauses for a second mid-sentence, a receptionist answers two questions in two bursts. The transcript of such a call reads fine. You only find the problem by listening to the recording with the agent's timings next to it.

So the useful thing to collect is the other side's audio on its own, with a transcript that says roughly when each line was said. That can be played back into the agent in a test, and the test can check when the agent spoke.

## With audio or transcript only

The trouble is that the interesting calls have someone else's voice on them: a receptionist, a salesperson, a support agent. They didn't agree to be recorded for anyone's project, let alone published.

So samples come in two versions:

- **Transcript only.** The redacted transcript with when each line was said. No audio. This is the version to share.
- **With original audio.** Each side as the PBX recorded it, plus the transcript. Keep it to yourself unless everyone on it agreed.

Earlier releases had a third, re-voiced version: every stretch of speech replaced by the TTS voice at the same timing. It's gone. It lost exactly what makes a hard call hard (the accent, the mumbling, a line that cuts words off into silence), and its audio was transcribed again on export, so it could say things the transcript you reviewed didn't. The transcript with timings carries the turn-taking just as well.

## What's in a sample

A sample is a zip:

| File | What it is |
| --- | --- |
| `other.wav` | The other side only (agent calls, with the recordings mounted) |
| `agent.wav` | The agent only |
| `call.wav` | Both sides mixed |
| `recording.wav` | A recording made outside the agent |
| `transcript.json` | Lines with `t` (seconds from the start), `who` and `text`, and `audio`: which audio files are in the zip |
| `transcript.txt` | The same, for reading |
| `README.txt` | What the files are, and which version this is |

Only the files that exist go in. Phone numbers, the brief and the recording's file name (FreePBX puts both numbers in it) aren't included.

## From a call the agent made or answered

The call page has a Download line under the player: the recording with credit, and the test sample **transcript only** or **with original audio**. The player and the audio need `RECORDINGS_DIR` pointing at the PBX's recordings (see [the FreePBX guide](asterisk-freepbx.md#12-recordings-in-the-container)). Without it there's a single link with the transcript and timings only.

From the command line:

```sh
docker compose exec phone-agent python -m phone_agent.sample --call 3f2a91c0 --no-audio -o /data/sample.zip
```

Leave out `--no-audio` to include the original audio. The call id can be shortened to its first characters. `/data` is the container's data folder, so the file ends up in `./data` next to your `docker-compose.yml`.

Calls recorded before 2026.11.1.1 have no timings in the transcript; their lines show `?`. The audio's timing is still exact.

## From any FreePBX recording

Calls you took yourself are often the best material: a salesperson who doesn't stop, a support line with a long menu. FreePBX keeps them under `/var/spool/asterisk/monitor/YYYY/MM/DD/`.

With that folder mounted as `RECORDINGS_DIR`:

```sh
docker compose exec phone-agent python -m phone_agent.sample --no-audio \
    --recording 2026/11/14/external-0255501234-0412000000-20261114-101501-1763082901.12.wav \
    --who caller,me -o /data/sample.zip
```

Or copy the file into `./data` first and pass `/data/<file>.wav`. The tool finds stretches of speech and transcribes each one with your Whisper server. Without `--no-audio` it also copies the original recording into the zip. A stereo recording is taken as one side per channel, and `--who` names them. A mono recording has a single `line` speaker for both people. Only 16-bit WAV is read: convert anything else with `sox in.gsm -b 16 out.wav` first.

## Before you share one

Read `transcript.txt`. Numbers, email addresses, long references (`BBA7Q2K9`, or one read out letter by letter) and the owner's name are replaced automatically. A name, a street or a company said in plain words isn't caught. If one is there, edit it out of both transcript files, or don't share the sample.

To contribute, open a [Test sample issue](https://github.com/darek-margas/ai-on-the-line/issues/new?template=test_sample.yml) and attach a **transcript-only** zip. Original audio is accepted only when every voice on it is yours, for example you playing a caller against your own screener. The project doesn't take original recordings of anyone else, publicly or privately. Samples added to the project are shared under CC BY-SA 4.0, like the rest of `docs/`.

---

Part of [AI on the Line](https://github.com/darek-margas/ai-on-the-line) by Darek Margas. This page is licensed under [CC BY-SA 4.0](LICENSE.md).
