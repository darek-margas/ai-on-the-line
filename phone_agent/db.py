# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Darek Margas. See NOTICE.
"""SQLite call history, so results survive restarts and the status page can show them."""

import json
import os
import sqlite3
import time
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS calls (
    id TEXT PRIMARY KEY,           -- also the AudioSocket UUID
    created REAL NOT NULL,
    phone TEXT NOT NULL,
    callee TEXT,
    brief TEXT NOT NULL,           -- JSON
    status TEXT NOT NULL,          -- awaiting_approval | scheduled | queued | dialing | connected
                                   -- | finished | not_answered | failed | cancelled
    outcome TEXT,
    answers TEXT,                  -- JSON {question: answer}
    notes TEXT,                    -- JSON list
    transcript TEXT,               -- JSON list of [speaker, text]
    timings TEXT,                  -- JSON list: seconds from start to each transcript line
    recording TEXT,
    started REAL,
    ended REAL,
    error TEXT,
    schedule TEXT                  -- JSON: run_at, calling-hours zone, approval
);
CREATE TABLE IF NOT EXISTS approved_numbers (
    number TEXT PRIMARY KEY,       -- as dialled (after the dialling rules)
    added REAL NOT NULL,
    call_id TEXT                   -- the call it was approved with
);
"""

JSON_COLUMNS = ("brief", "answers", "notes", "transcript", "timings", "schedule")
PENDING = ("awaiting_approval", "scheduled")


class Database:
    def __init__(self, data_dir: str):
        os.makedirs(data_dir, exist_ok=True)
        self.conn = sqlite3.connect(os.path.join(data_dir, "phone-agent.db"), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(SCHEMA)
        columns = {row[1] for row in self.conn.execute("PRAGMA table_info(calls)")}
        if "timings" not in columns:  # databases from before 2026.11.1.1
            self.conn.execute("ALTER TABLE calls ADD COLUMN timings TEXT")
        if "schedule" not in columns:  # databases from before 2026.11.2.0
            self.conn.execute("ALTER TABLE calls ADD COLUMN schedule TEXT")
        self.conn.commit()

    def fail_unfinished_calls(self) -> None:
        """At service start: calls that were live when it stopped can't be resumed.
        Calls still waiting for approval or their time are picked up again."""
        self.conn.execute(
            "UPDATE calls SET status = 'failed', error = 'service restarted' "
            "WHERE status IN ('queued', 'dialing', 'ringing', 'connected')"
        )
        self.conn.commit()

    def pending_calls(self) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            f"SELECT * FROM calls WHERE status IN ({', '.join('?' for _ in PENDING)}) ORDER BY created", PENDING
        ).fetchall()
        return [self._decode(r) for r in rows]

    def number_approved(self, number: str) -> bool:
        return self.conn.execute("SELECT 1 FROM approved_numbers WHERE number = ?", (number,)).fetchone() is not None

    def approve_number(self, number: str, call_id: str) -> None:
        self.conn.execute("INSERT OR IGNORE INTO approved_numbers VALUES (?, ?, ?)", (number, time.time(), call_id))
        self.conn.commit()

    def save_call(self, call: dict[str, Any]) -> None:
        row = {k: json.dumps(v) if k in JSON_COLUMNS else v for k, v in call.items()}
        cols = ", ".join(row)
        marks = ", ".join("?" for _ in row)
        self.conn.execute(f"INSERT OR REPLACE INTO calls ({cols}) VALUES ({marks})", tuple(row.values()))
        self.conn.commit()

    def _decode(self, row: sqlite3.Row) -> dict[str, Any]:
        call = dict(row)
        for key in JSON_COLUMNS:
            call[key] = json.loads(call[key]) if call[key] else None
        return call

    def get_call(self, call_id: str) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT * FROM calls WHERE id = ?", (call_id,)).fetchone()
        return self._decode(row) if row else None

    def recent_calls(self, limit: int = 50) -> list[dict[str, Any]]:
        rows = self.conn.execute("SELECT * FROM calls ORDER BY created DESC LIMIT ?", (limit,)).fetchall()
        return [self._decode(r) for r in rows]
