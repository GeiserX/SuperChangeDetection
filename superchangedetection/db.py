"""State the bot keeps, in SQLite under the data directory: the chat history the AI gets as a
recap, the lines not yet sent to the AI, one conversation id per chat, the open delete offer, the
pending first checks and the Telegram update offset. Never a copy of the watches.

A chat row keeps the Telegram message id (`mid`) and the message's own words (`body`), so an edit
finds it and says what it said before; `asked` is 1 for a message that called the bot, 0 for one
it only saw, and NULL for the bot's and the app's own lines. An unsent row holds the same as JSON,
with `asked` (absent in rows from before 0.3.0: they all called the bot)."""

from __future__ import annotations

import datetime as dt
import json
import sqlite3
import threading
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS chat (
  id INTEGER PRIMARY KEY AUTOINCREMENT, chat INTEGER NOT NULL, ts TEXT NOT NULL,
  role TEXT NOT NULL, who TEXT, text TEXT NOT NULL, mid INTEGER, asked INTEGER, body TEXT
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
            cols = {r["name"] for r in c.execute("PRAGMA table_info(chat)")}
            for col, kind in (("mid", "INTEGER"), ("asked", "INTEGER"), ("body", "TEXT")):
                if col not in cols:  # databases from before 0.3.0
                    c.execute(f"ALTER TABLE chat ADD COLUMN {col} {kind}")

    def conn(self) -> sqlite3.Connection:
        c = sqlite3.connect(self.path, timeout=30)
        c.row_factory = sqlite3.Row
        return c

    # ---- history (the recap)
    def add_chat(
        self,
        chat: int,
        role: str,
        who: str | None,
        text: str,
        mid: int | None = None,
        asked: bool | None = None,
        body: str | None = None,
    ) -> int:
        with self._lock, self.conn() as c:
            cur = c.execute(
                "INSERT INTO chat (chat, ts, role, who, text, mid, asked, body) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    chat,
                    dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
                    role,
                    who,
                    text,
                    mid,
                    None if asked is None else int(asked),
                    body,
                ),
            )
            return int(cur.lastrowid)

    def chat_rows(self, chat: int, limit: int, days: int = 0) -> list[dict]:
        """The last `limit` rows of a chat, and with `days` every row of those last days, oldest
        first."""
        since = (dt.datetime.now(dt.UTC) - dt.timedelta(days=days)).isoformat(timespec="seconds")
        with self.conn() as c:
            rows = c.execute(
                "SELECT * FROM chat WHERE chat = ? AND (id IN (SELECT id FROM chat WHERE chat = ? "
                "ORDER BY id DESC LIMIT ?) OR (? AND ts >= ?)) ORDER BY id",
                (chat, chat, limit, int(days > 0), since),
            ).fetchall()
        return [dict(r) for r in rows]

    def history(self, chat: int, limit: int, days: int = 0) -> list[dict]:
        """The recent lines as the AI's messages: [{"role": user|assistant|app, "content", "ts"}].
        A message the bot only saw goes in as `chat: <who>: <text>`."""
        return [
            {
                "role": r["role"],
                "content": ("chat: " if r["asked"] == 0 else "") + r["text"],
                "ts": r["ts"],
            }
            for r in self.chat_rows(chat, limit, days)
        ]

    def find_chat(self, chat: int, mid: int) -> dict | None:
        """The logged message with this Telegram message id, or None."""
        with self.conn() as c:
            r = c.execute(
                "SELECT * FROM chat WHERE chat = ? AND mid = ? AND role = 'user' "
                "ORDER BY id DESC LIMIT 1",
                (chat, mid),
            ).fetchone()
        return dict(r) if r else None

    def answer_after(self, chat: int, row: int) -> str | None:
        """The bot's first answer after this row: what an edit of an answered message questions."""
        with self.conn() as c:
            r = c.execute(
                "SELECT text FROM chat WHERE chat = ? AND id > ? AND role = 'assistant' "
                "ORDER BY id LIMIT 1",
                (chat, row),
            ).fetchone()
        return r["text"] if r else None

    def edit_chat(self, row: int, text: str, body: str) -> None:
        """An edited message keeps its row, so later turns and recaps see its final wording."""
        with self._lock, self.conn() as c:
            c.execute("UPDATE chat SET text = ?, body = ? WHERE id = ?", (text, body, row))

    def delete_chat(self, row: int) -> None:
        with self._lock, self.conn() as c:
            c.execute("DELETE FROM chat WHERE id = ?", (row,))

    def clear_chat(self, chat: int) -> None:
        with self._lock, self.conn() as c:
            c.execute("DELETE FROM chat WHERE chat = ?", (chat,))
            c.execute("DELETE FROM unsent WHERE chat = ?", (chat,))

    # ---- lines waiting for the AI
    def push_unsent(self, chat: int, row: dict, keep_seen: int = 200) -> int:
        """Queue a line; a chat keeps at most `keep_seen` lines it only saw, the oldest go."""
        with self._lock, self.conn() as c:
            cur = c.execute("INSERT INTO unsent (chat, row) VALUES (?, ?)", (chat, json.dumps(row)))
            if row.get("asked") is False:
                c.execute(
                    "DELETE FROM unsent WHERE chat = ? AND json_extract(row, '$.asked') = 0 AND id "
                    "NOT IN (SELECT id FROM unsent WHERE chat = ? AND json_extract(row, '$.asked') "
                    "= 0 ORDER BY id DESC LIMIT ?)",
                    (chat, chat, keep_seen),
                )
            return int(cur.lastrowid)

    def update_unsent(self, row: dict) -> None:
        """Write a queued line again (an edit of it), keeping its place in the queue."""
        body = {k: v for k, v in row.items() if k != "_id"}
        with self._lock, self.conn() as c:
            c.execute("UPDATE unsent SET row = ? WHERE id = ?", (json.dumps(body), row["_id"]))

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
