"""State the bot keeps: the chat history the AI sees, one conversation id per chat, pending
confirmations and the Telegram update offset. SQLite under the data directory."""

from __future__ import annotations

import datetime as dt
import json
import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS chat (
  id INTEGER PRIMARY KEY AUTOINCREMENT, chat INTEGER NOT NULL, ts TEXT NOT NULL,
  role TEXT NOT NULL, who TEXT, text TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""


class DB:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.conn() as c:
            c.executescript(SCHEMA)

    def conn(self) -> sqlite3.Connection:
        c = sqlite3.connect(self.path)
        c.row_factory = sqlite3.Row
        return c

    def add_chat(self, chat: int, role: str, who: str | None, text: str) -> int:
        with self.conn() as c:
            cur = c.execute(
                "INSERT INTO chat (chat, ts, role, who, text) VALUES (?, ?, ?, ?, ?)",
                (chat, dt.datetime.now(dt.UTC).isoformat(timespec="seconds"), role, who, text),
            )
            return int(cur.lastrowid)

    def chat_rows(self, chat: int, limit: int) -> list[dict]:
        with self.conn() as c:
            rows = c.execute(
                "SELECT * FROM chat WHERE chat = ? ORDER BY id DESC LIMIT ?", (chat, limit)
            ).fetchall()
        return [dict(r) for r in reversed(rows)]

    def clear_chat(self, chat: int) -> None:
        with self.conn() as c:
            c.execute("DELETE FROM chat WHERE chat = ?", (chat,))

    def get_kv(self, key: str):
        with self.conn() as c:
            row = c.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
        return json.loads(row["value"]) if row else None

    def set_kv(self, key: str, value) -> None:
        with self.conn() as c:
            if value is None:
                c.execute("DELETE FROM kv WHERE key = ?", (key,))
            else:
                c.execute(
                    "INSERT OR REPLACE INTO kv (key, value) VALUES (?, ?)", (key, json.dumps(value))
                )
