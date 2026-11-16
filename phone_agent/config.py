# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Darek Margas. See NOTICE.
"""Settings, read once from environment variables (see .env.example)."""

import os
import re
from dataclasses import dataclass, field

from .dialing import TYPES, DialRules


def _env(name: str, default: str | None = None) -> str:
    value = os.environ.get(name, default)
    if value is None:
        raise RuntimeError(f"Missing required environment variable {name}")
    return value


def _host_port(value: str, default_port: int) -> tuple[str, int]:
    host, _, port = value.partition(":")
    return host, int(port or default_port)


@dataclass(frozen=True)
class Config:
    data_dir: str
    http_host: str
    http_port: int

    # AudioSocket server; ADVERTISE is how Asterisk reaches us (host:port).
    audiosocket_bind: tuple[str, int]
    audiosocket_advertise: str
    # Call audio rate; must match c(slinNN) in the dialplan's Dial(AudioSocket/...).
    audio_rate: int

    # Asterisk REST Interface on the FreePBX box.
    ari_url: str
    ari_user: str
    ari_password: str
    # {number} is replaced. Local/.../n goes through FreePBX outbound routes.
    dial_endpoint: str
    caller_id: str
    # Optional AMI over HTTP (http://pbx:8088/rawman), for real DTMF key presses.
    ami_url: str
    ami_user: str
    ami_password: str
    ring_timeout_seconds: int

    # Wyoming services (the same containers Home Assistant uses).
    stt: tuple[str, int]
    stt_language: str
    tts: tuple[str, int]
    tts_voice: str
    # How the voice should say awkward words: "Siobhan=Shivawn,Nguyen=Win".
    tts_pronounce: dict[str, str]

    # In-call LLM: "claude:<model>" or "ollama:<model>".
    dialog_llm: str
    ollama_url: str
    # Every request resets how long Ollama keeps the model loaded; match what
    # Home Assistant uses so we never shorten it. -1 = keep loaded forever.
    ollama_keep_alive: str

    owner_name: str
    timezone: str
    call_hours: tuple[str, str]
    max_concurrent_calls: int
    max_call_seconds: int
    hold_timeout_seconds: int
    barge_in: bool

    recordings_dir: str
    # Shared secret for the HTTP port (MCP, status pages, recordings). Empty = no auth.
    api_token: str = ""
    # Incoming calls the PBX hands over (POST /inbound): how many at once, and how long each may run.
    max_inbound_calls: int = 1
    inbound_max_seconds: int = 900
    # How the agent refers to the owner (he, she or they), and what the call
    # screener may say about them. Empty bio = no small talk about the owner.
    owner_pronouns: str = "they"
    owner_bio: str = ""
    # Downloaded recordings: a spoken "Recorded with AI on the Line." at the end,
    # and the voice named in the credit line (empty if you use another voice).
    export_spoken_credit: bool = True
    credit_voice: str = "Jenny (Dioco)"
    # The call screener tells callers the call is recorded (in its greeting).
    screener_recording_notice: bool = True
    # Which numbers may be dialled (DIAL_* settings; see docs/dialling.md).
    dial_rules: DialRules = field(default_factory=DialRules)
    # Who approves outgoing calls (see SECURITY.md). "off": they dial at once.
    # "new_numbers": extensions, APPROVED_NUMBERS and numbers approved before dial
    # at once; anything else waits for the owner on the call page. "all": every
    # call waits. Approving needs APPROVAL_TOKEN, which Claude must not have.
    call_approval: str = "off"
    approval_token: str = ""
    approved_numbers: list[str] = field(default_factory=list)
    approval_hours: float = 24.0  # an unapproved call is dropped this long after it was due
    # Longest time on hold (queue music and messages) before giving up. Hold time
    # doesn't count against max_call_seconds.
    max_hold_seconds: int = 1800
    # In-call LLM for incoming calls, same format as dialog_llm. Empty = use dialog_llm.
    inbound_dialog_llm: str = ""

    @classmethod
    def from_env(cls) -> "Config":
        start, _, end = _env("CALL_HOURS", "08:30-18:30").partition("-")
        return cls(
            data_dir=_env("DATA_DIR", "./data"),
            http_host=_env("HTTP_HOST", "0.0.0.0"),
            http_port=int(_env("HTTP_PORT", "8090")),
            audiosocket_bind=_host_port(_env("AUDIOSOCKET_BIND", "0.0.0.0:9092"), 9092),
            audiosocket_advertise=_env("AUDIOSOCKET_ADVERTISE"),
            audio_rate=int(_env("AUDIO_RATE", "16000")),
            ari_url=_env("ARI_URL").rstrip("/"),
            ari_user=_env("ARI_USER"),
            ari_password=_env("ARI_PASSWORD"),
            dial_endpoint=_env("DIAL_ENDPOINT", "Local/{number}@from-internal/n"),
            caller_id=_env("CALLER_ID", ""),
            ami_url=_env("AMI_URL", ""),
            ami_user=_env("AMI_USER", ""),
            ami_password=_env("AMI_PASSWORD", ""),
            ring_timeout_seconds=int(_env("RING_TIMEOUT_SECONDS", "45")),
            stt=_host_port(_env("WYOMING_STT", "localhost:10300"), 10300),
            stt_language=_env("STT_LANGUAGE", "en"),
            tts=_host_port(_env("WYOMING_TTS", "localhost:10200"), 10200),
            tts_voice=_env("TTS_VOICE", ""),
            tts_pronounce=dict(
                pair.split("=", 1) for pair in _env("TTS_PRONOUNCE", "").split(",") if "=" in pair
            ),
            dialog_llm=_env("DIALOG_LLM", "claude:claude-haiku-5-5"),
            ollama_url=_env("OLLAMA_URL", "http://localhost:11434").rstrip("/"),
            ollama_keep_alive=_env("OLLAMA_KEEP_ALIVE", "-1"),
            owner_name=_env("OWNER_NAME"),
            timezone=_env("TIMEZONE", "UTC"),
            call_hours=(start, end),
            max_concurrent_calls=int(_env("MAX_CONCURRENT_CALLS", "1")),
            max_call_seconds=int(_env("MAX_CALL_SECONDS", "480")),
            hold_timeout_seconds=int(_env("HOLD_TIMEOUT_SECONDS", "240")),
            barge_in=_env("BARGE_IN", "true").lower() == "true",
            recordings_dir=_env("RECORDINGS_DIR", ""),
            api_token=_env("API_TOKEN", "").strip(),
            max_inbound_calls=int(_env("MAX_INBOUND_CALLS", "1")),
            inbound_max_seconds=int(_env("INBOUND_MAX_SECONDS", "900")),
            owner_pronouns=_env("OWNER_PRONOUNS", "they"),
            owner_bio=_env("OWNER_BIO", ""),
            export_spoken_credit=_env("EXPORT_SPOKEN_CREDIT", "true").lower() == "true",
            credit_voice=_env("CREDIT_VOICE", "Jenny (Dioco)"),
            screener_recording_notice=_env("SCREENER_RECORDING_NOTICE", "true").lower() == "true",
            dial_rules=_dial_rules(),
            inbound_dialog_llm=_env("INBOUND_DIALOG_LLM", "").strip(),
            max_hold_seconds=int(_env("MAX_HOLD_SECONDS", "1800")),
            **_approval(),
        )


APPROVAL_MODES = ("off", "new_numbers", "all")


def _approval() -> dict:
    token = _env("APPROVAL_TOKEN", "").strip()
    mode = _env("CALL_APPROVAL", "").strip().lower() or ("new_numbers" if token else "off")
    if mode not in APPROVAL_MODES:
        raise ValueError(f"CALL_APPROVAL must be one of {', '.join(APPROVAL_MODES)}, not {mode!r}")
    if mode != "off" and not token:
        raise ValueError(f"CALL_APPROVAL={mode} needs APPROVAL_TOKEN: the secret you sign in with to approve calls")
    if token and token == _env("API_TOKEN", "").strip():
        raise ValueError("APPROVAL_TOKEN must differ from API_TOKEN: Claude has API_TOKEN")
    return {
        "call_approval": mode,
        "approval_token": token,
        "approved_numbers": [n.strip() for n in _env("APPROVED_NUMBERS", "").split(",") if n.strip()],
        "approval_hours": float(_env("APPROVAL_HOURS", "24")),
    }


def _dial_rules() -> DialRules:
    defaults = DialRules()
    allow = {t.strip().lower() for t in _env("DIAL_ALLOW", ",".join(sorted(defaults.allow))).split(",") if t.strip()}
    unknown = allow - TYPES.keys()
    if unknown:
        raise RuntimeError(f"DIAL_ALLOW: unknown number types {sorted(unknown)}; known: {', '.join(TYPES)}")
    rules = DialRules(
        country=_env("DIAL_COUNTRY", defaults.country).strip().upper(),
        allow=allow,
        allow_international=_env("DIAL_INTERNATIONAL", "false").lower() == "true",
        local_area_code=_env("DIAL_LOCAL_AREA_CODE", "").strip(),
        extensions=_env("DIAL_EXTENSIONS", defaults.extensions).strip(),
        allow_pattern=_env("DIAL_ALLOW_PATTERN", "").strip(),
        block_pattern=_env("DIAL_BLOCK_PATTERN", "").strip(),
        dial_format=_env("DIAL_FORMAT", defaults.dial_format).strip().lower(),
    )
    if rules.dial_format not in ("national", "e164"):
        raise RuntimeError("DIAL_FORMAT must be national or e164")
    for name, pattern in (("DIAL_EXTENSIONS", rules.extensions), ("DIAL_ALLOW_PATTERN", rules.allow_pattern),
                          ("DIAL_BLOCK_PATTERN", rules.block_pattern)):
        try:
            re.compile(pattern)
        except re.error as e:
            raise RuntimeError(f"{name}: {e}") from None
    return rules
