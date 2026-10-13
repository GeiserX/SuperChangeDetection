"""State the bot keeps, in SQLite under the data directory: the chat history the AI gets as a
recap, the lines not yet sent to the AI, one conversation id per chat, the open delete offer, the
pending first checks and the Telegram update offset. Never a copy of the watches."""

from __future__ import annotations

import datetime as dt
import json
import sqlite3
import threading
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS chat (
  id INTEGER PRIMARY KEY AUTOINCREMENT, chat INTEGER NOT NULL, ts TEXT NOT NULL,
  role TEXT NOT NULL, who TEXT, text TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS unsent (
  id INTEGER PRIMARY KEY AUTOINCREMENT, chat INTEGER NOT NULL, row TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS followup (
  id INTEGER PRIMARY KEY AUTOINCREMENT, row TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""


class DB:
    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.conn() as c:
            c.executescript(SCHEMA)

    def conn(self) -> sqlite3.Connection:
        c = sqlite3.connect(self.path, timeout=30)
        c.row_factory = sqlite3.Row
        return c

    # ---- history (the recap)
    def add_chat(self, chat: int, role: str, who: str | None, text: str) -> int:
        with self._lock, self.conn() as c:
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

    def history(self, chat: int, limit: int) -> list[dict]:
        """The recent lines as the AI's messages: [{"role": user|assistant|app, "content"}]."""
        return [{"role": r["role"], "content": r["text"]} for r in self.chat_rows(chat, limit)]

    def clear_chat(self, chat: int) -> None:
        with self._lock, self.conn() as c:
            c.execute("DELETE FROM chat WHERE chat = ?", (chat,))
            c.execute("DELETE FROM unsent WHERE chat = ?", (chat,))

    # ---- lines waiting for the AI
    def push_unsent(self, chat: int, row: dict) -> None:
        with self._lock, self.conn() as c:
            c.execute("INSERT INTO unsent (chat, row) VALUES (?, ?)", (chat, json.dumps(row)))

    def unsent(self, chat: int) -> list[dict]:
        with self.conn() as c:
            rows = c.execute("SELECT * FROM unsent WHERE chat = ? ORDER BY id", (chat,)).fetchall()
        return [{**json.loads(r["row"]), "_id": r["id"]} for r in rows]

    def drop_unsent(self, chat: int, rows: list[dict]) -> None:
        ids = [r["_id"] for r in rows if "_id" in r]
        if not ids:
            return
        with self._lock, self.conn() as c:
            c.executemany("DELETE FROM unsent WHERE id = ?", [(i,) for i in ids])

    def chats_with_unsent(self) -> list[int]:
        with self.conn() as c:
            return [int(r[0]) for r in c.execute("SELECT DISTINCT chat FROM unsent").fetchall()]

    # ---- first checks to look at again
    def add_followup(self, row: dict) -> None:
        with self._lock, self.conn() as c:
            c.execute("INSERT INTO followup (row) VALUES (?)", (json.dumps(row),))

    def followups(self) -> list[dict]:
        with self.conn() as c:
            rows = c.execute("SELECT * FROM followup ORDER BY id").fetchall()
        return [{**json.loads(r["row"]), "_id": r["id"]} for r in rows]

    def drop_followup(self, row: dict) -> None:
        with self._lock, self.conn() as c:
            c.execute("DELETE FROM followup WHERE id = ?", (row["_id"],))

    # ---- small values
    def get_kv(self, key: str):
        with self.conn() as c:
            row = c.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
        return json.loads(row["value"]) if row else None

    def set_kv(self, key: str, value) -> None:
        with self._lock, self.conn() as c:
            if value is None:
                c.execute("DELETE FROM kv WHERE key = ?", (key,))
            else:
                c.execute(
                    "INSERT OR REPLACE INTO kv (key, value) VALUES (?, ?)", (key, json.dumps(value))
                )
