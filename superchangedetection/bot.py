"""The chat: who the bot answers, what each message becomes for the AI, and what the app does with
the answer.

- A private chat with one of `bot.users` is always answered. In the group (`bot.chat_id`) only a
  message that mentions the bot's @handle or replies to one of its messages is; a change
  notification comes from changedetection as the bot, so replying to one counts, and its last line
  `ref <uuid>` ties the reply to the watch.
- Each chat is one conversation for the AI (a plugin keeps it on its side; the other providers get
  the recent lines each time). Lines wait in SQLite until the AI has taken them, so a restart
  loses nothing.
- One answer per burst: a chat's turn starts DEBOUNCE_S after its newest message (and waits up to
  VOICE_WAIT_S for a voice note of the chat still being transcribed), so messages sent together go
  as one turn. What calls the bot while a turn runs is handed to it (turn["incoming"]); the lines
  its answer took in are named in `covers`, the rest make the next turn. An answer that covers one
  message replies to it; one that covers several is sent plainly.
- Edits (edited_message) are handled by where the message is: still waiting, its new text goes; in
  the running turn, a correction with the old and the new wording joins it; already answered, the
  correction starts a new turn with the answer it got; a group message that did not call the bot and
  now mentions it is answered. An edit is never a command and never a yes or a no.
- With bot.history_scope "all", the group messages that do not call the bot go to the AI too, as
  context marked `chat:`, with the next turn. Telegram shows a bot those (and their edits) only when
  it is a group admin or privacy mode is off.
- The AI's answer is shown as it is; its actions block is validated, creates, updates and rechecks
  run at once and the chat gets what each watch sees now; a delete is offered with two buttons and
  runs only after the person who asked says yes, within the hour.
- A create or update whose first check had not finished is followed up: the bot looks again every
  half minute and posts what the watch sees once the check ran.
- A voice note or audio file from someone allowed is always answered, in the group too (it cannot
  mention the bot or reply by itself), caption or not: the transcript is echoed as a reply, then
  handled as typed text.
- A proposal from the provider (something it would do outside the chat, such as changing the
  bot's own code) is shown; `confirm_by` none runs at once in the background, the others wait for
  a yes from the asker or from one of `bot.users`. Only `bot.users` may ask for one.
- At start every watch the bot owns gets the app's current notification template, so a template
  change in the locale reaches the old watches too.

The service log gets event kinds and counts, never a message text or a watch's content.
"""

from __future__ import annotations

import datetime as dt
import html
import logging
import re
import threading
import time
import unicodedata
import uuid
from dataclasses import dataclass, field
from zoneinfo import ZoneInfo

from . import actions, context, llm, render
from .actions import ActionError
from .cd import CD, CDError, notifies
from .db import DB
from .i18n import Translator
from .model import Data
from .telegram import Telegram, TelegramError, safe

log = logging.getLogger("superchangedetection.bot")

POLL_S = 20  # getUpdates long poll
WAIT_409_S = 30  # another process polls this token
OFFER_TTL_S = 3600  # a delete offer answers only within the hour
FOLLOWUP_EVERY_S = 30
FOLLOWUP_GIVE_UP_S = 30 * 60
TYPING_S = 4
# a chat's turn starts this long after its newest message, so a burst goes together
DEBOUNCE_S = 2.5
VOICE_WAIT_S = 30  # a turn waits this long at most for a voice note of its chat still transcribing
SETTLE_STEP_S = 0.5
ANSWER_CLIP = 1500  # characters of an earlier answer quoted back with an edit of its message
REF = re.compile(r"(?m)^ref ([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\s*$")
COMMAND = re.compile(r"^/([A-Za-z0-9_]+)(?:@([A-Za-z0-9_]+))?(?:\s+(.*))?$", re.DOTALL)
COMMANDS = {
    "start": "help",
    "help": "help",
    "ayuda": "help",
    "list": "list",
    "lista": "list",
    "watches": "list",
    "new": "new",
    "nuevo": "new",
    "chatid": "chatid",
    "presentacion": "intro",
    "intro": "intro",
}
RUNNING = "running"  # proposals being carried out now, across every chat
YES = {"si", "yes", "vale", "ok", "s", "y", "dale"}
NO = {"no", "n", "nope"}


def _word(text: str) -> str:
    s = unicodedata.normalize("NFKD", text.strip().lower())
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"[^a-z]", "", s)


def _now() -> int:
    return int(time.time())


@dataclass
class Turn:
    """The turn a chat is running: the queued lines it started with and those handed to it since."""

    chat: int
    ids: set[int]
    pulled: list[int] = field(default_factory=list)
    closed: bool = False

    def taken(self) -> set[int]:
        return self.ids | set(self.pulled)


def _asked(row: dict) -> bool:
    return row.get("asked", True) is not False


def _reply_to(rows: list[dict]) -> int | None:
    """An answer replies to the message it answers only when it answers exactly one (an edit's
    correction is about the message it edits, so it counts as that one)."""
    mids = {r.get("msg") for r in rows if _asked(r)}
    return mids.pop() if len(mids) == 1 else None


def _iso(ts) -> str:
    return dt.datetime.fromtimestamp(int(ts or 0), dt.UTC).isoformat(timespec="seconds")


def _same(a: str, b: str) -> bool:
    return " ".join((a or "").split()) == " ".join((b or "").split())


class Worker:
    """One thread per chat: runs `fn(chat)` whenever kicked, never two turns of a chat at once."""

    def __init__(self, chat: int, fn):
        self.chat, self.fn = chat, fn
        self.cv = threading.Condition()
        self.kicked = False
        self.thread = threading.Thread(target=self._loop, name=f"scd-chat-{chat}", daemon=True)
        self.thread.start()

    def kick(self) -> None:
        with self.cv:
            self.kicked = True
            self.cv.notify()

    def _loop(self) -> None:
        while True:
            with self.cv:
                while not self.kicked:
                    self.cv.wait()
                self.kicked = False
            try:
                self.fn(self.chat)
            except Exception as e:  # noqa: BLE001 - a turn's failure never ends the chat's thread
                log.error("turn failed for chat %s: %s", self.chat, safe(e))


class Bot:
    def __init__(self, data: Data, tg: Telegram, cd: CD):
        self.data, self.tg, self.cd = data, tg, cd
        self.s = data.settings
        self.t = Translator(self.s.locale)
        self.token = data.secret(self.s.bot.token_env)
        self.db = DB(data.state / "bot.sqlite")
        self.me: dict = {}
        self.mentions_ok = True  # the group's @mentions reach the bot (admin, or privacy off)
        self.workers: dict[int, Worker] = {}
        self._lock = threading.Lock()
        self.debounce, self.voice_wait, self.clock = DEBOUNCE_S, VOICE_WAIT_S, time.monotonic
        # what the turn loop, incoming() and an edit look at together: guarded by qlock
        self.qlock = threading.Lock()
        self.arrived: dict[int, float] = {}  # chat -> when its newest line that calls the bot came
        self.voicing: dict[int, list[float]] = {}  # chat -> start of each note being transcribed
        self.running: dict[int, Turn] = {}

    # ---- start and run

    @property
    def handle(self) -> str:
        return self.me.get("username") or ""

    def start(self) -> None:
        self.me = self.tg.get_me()
        log.info("bot @%s up", self.handle)
        if self.s.bot.sync_profile:
            try:
                self.tg.set_profile(
                    self.s.locale,
                    self.t("commands"),
                    self.t("description"),
                    self.t("short_description"),
                )
            except TelegramError as e:
                log.warning("profile sync failed: %s", safe(e))
        self.check_group()
        self.sync_notifications()
        self.interrupted()

    def check_group(self) -> None:
        """Whether the group's @mentions reach the bot: they do not with privacy mode on unless it
        is an admin of the group."""
        group = self.group()
        if not group or self.me.get("can_read_all_group_messages"):
            return
        try:
            m = self.tg.get_chat_member(group, self.me.get("id"))
        except TelegramError as e:
            log.info("could not check the bot's rights in the group: %s", safe(e))
            return
        if m.get("status") not in ("administrator", "creator"):
            self.mentions_ok = False
            log.warning(
                "privacy mode is on and the bot is not an admin of the group: it sees "
                "commands and replies, not @mentions, other messages or their edits (make it "
                "an admin, or /setprivacy)"
            )

    def sync_notifications(self) -> None:
        """Every watch the bot owns carries the app's current notification template (title, body,
        format), so a change in the locale reaches the watches made before it."""
        try:
            ws = self.cd.watches()
        except CDError as e:
            log.warning("notification sync skipped: %s", safe(e))
            return
        n = 0
        for uuid_, w in ws.items():
            chat = notifies(w, self.token)
            if chat is None:
                continue
            want = actions.notification(self.s, self.token, chat, self.t)
            fields = {
                k: v
                for k, v in want.items()
                if k.startswith("notification_")
                and k not in ("notification_urls", "notification_muted")
                if w.get(k) != v
            }
            if not fields:
                continue
            try:
                self.cd.update(uuid_, fields)
                n += 1
            except CDError as e:
                log.warning("notification sync failed for a watch: %s", safe(e))
        if n:
            log.info("notification template updated on %d watches", n)

    def later(self, fn, *args) -> None:
        """Run `fn` on its own thread: a transcription or a proposal must not hold the poll."""
        threading.Thread(target=fn, args=args, daemon=True).start()

    def run(self, stop: threading.Event) -> None:
        self.start()
        threading.Thread(
            target=self._followups, args=(stop,), name="scd-followups", daemon=True
        ).start()
        for chat in self.db.chats_with_unsent():
            self.worker(chat).kick()
        while not stop.is_set():
            wait = self.poll_once()
            if wait:
                stop.wait(wait)

    def poll_once(self) -> float:
        offset = self.db.get_kv("offset")
        try:
            updates = self.tg.get_updates(int(offset) if offset else None, POLL_S)
        except TelegramError as e:
            log.warning("getUpdates failed: %s", safe(e))
            if e.code == 401:
                raise
            return WAIT_409_S if e.code == 409 else min(e.retry_after or 5, 60)
        for u in updates or []:
            try:
                self.handle_update(u)
            except Exception as e:  # noqa: BLE001 - one bad update never stops the poll
                log.error("update failed: %s", safe(e))
            self.db.set_kv("offset", int(u["update_id"]) + 1)
        return 0

    def worker(self, chat: int) -> Worker:
        with self._lock:
            if chat not in self.workers:
                self.workers[chat] = Worker(chat, self.turn)
            return self.workers[chat]

    # ---- who

    def group(self) -> int | None:
        moved = self.db.get_kv("group")
        return int(moved) if moved else self.s.bot.chat_id

    def allowed(self, chat: dict, user: dict) -> bool:
        if chat.get("type") == "private":
            return user.get("id") in self.s.bot.users
        if chat.get("type") in ("group", "supergroup"):
            return chat.get("id") == self.group()
        return False

    def who(self, user: dict) -> str:
        handle = self.s.bot.handles.get(str(user.get("id")))
        return handle or user.get("first_name") or user.get("username") or "someone"

    def from_me(self, msg: dict | None) -> bool:
        return bool(msg) and ((msg.get("from") or {}).get("id") == self.me.get("id"))

    # ---- updates

    def handle_update(self, u: dict) -> None:
        if "message" in u:
            self.on_message(u["message"])
        elif "edited_message" in u:
            self.on_edit(u["edited_message"])
        elif "callback_query" in u:
            self.on_callback(u["callback_query"])

    def on_message(self, msg: dict) -> None:
        chat, user = msg.get("chat") or {}, msg.get("from") or {}
        if msg.get("migrate_to_chat_id") and chat.get("id") == self.group():
            self.db.set_kv("group", int(msg["migrate_to_chat_id"]))
            log.warning("the group became a supergroup; update bot.chat_id when convenient")
            return
        text = msg.get("text") or msg.get("caption") or ""
        voice = msg.get("voice") or msg.get("audio")
        if voice and self.allowed(chat, user) and self.s.transcription.provider != "none":
            start = self.clock()  # from now the chat's next turn waits for this note
            with self.qlock:
                self.voicing.setdefault(int(chat["id"]), []).append(start)
            self.later(self.voice, msg, start)
            return
        if not text or not self.allowed(chat, user):
            return
        chat_id = int(chat["id"])
        m = COMMAND.match(text.strip())
        if m and (not m.group(2) or m.group(2).lower() == self.handle.lower()):
            name = COMMANDS.get(m.group(1).lower())
            if name:
                getattr(self, f"cmd_{name}")(chat_id, msg)
            return
        private = chat.get("type") == "private"
        if self.answer_offer(chat_id, msg, private):
            return
        content = self.addressed(msg, text, private)
        if content is None:
            if not private and self.s.bot.history_scope == "all":
                self.enqueue(chat_id, msg, text.strip(), asked=False)
            return
        self.enqueue(chat_id, msg, content)

    def enqueue(
        self, chat_id: int, msg: dict, content: str, asked: bool = True, edit: bool = False
    ) -> None:
        """A line for the AI, tied to the watch when the message replies to a notification. A line
        that does not call the bot (asked False) waits for the next turn as context; edit True is
        the app's correction about msg, never msg itself."""
        user = msg.get("from") or {}
        ref = None
        if self.from_me(msg.get("reply_to_message")):
            found = REF.search(msg["reply_to_message"].get("text") or "")
            ref = found.group(1) if found else None
        row = {
            "who": self.who(user),
            "user": user.get("id"),
            "text": content,
            "ref": ref,
            "msg": msg.get("message_id"),
            "ts": _now(),
            "asked": asked,
        }
        if edit:
            row["edit"] = True
        self.db.push_unsent(chat_id, row)
        if asked:
            with self.qlock:
                self.arrived[chat_id] = self.clock()
            self.worker(chat_id).kick()

    def voice(self, msg: dict, start: float | None = None) -> None:
        """A voice note or audio file: fetched from Telegram, transcribed, echoed as a reply so
        everyone sees what was heard, then handled as typed text with its caption, if any. A failure
        tells the chat. Until it is done the chat's next turn waits for it (VOICE_WAIT_S at most)."""
        chat_id = int((msg.get("chat") or {})["id"])
        voice = msg.get("voice") or msg.get("audio") or {}
        try:
            try:
                f = self.tg.get_file(voice.get("file_id"))
                path = str(f.get("file_path") or "")
                name = path.rsplit("/", 1)[-1] or "voice.ogg"
                text = llm.transcribe(self.data, self.tg.download(path), name)
            except Exception as e:  # noqa: BLE001 - the provider's text may quote anything
                log.warning("transcription failed for chat %s: %s", chat_id, safe(e))
                text = ""
            if not text:
                self.send(
                    chat_id, self.t("voice_failed"), reply_to=msg.get("message_id"), plain=True
                )
                return
            self.send(chat_id, f"🎤 {text}", reply_to=msg.get("message_id"), plain=True)
            caption = self.clean(msg.get("caption") or "")
            self.enqueue(chat_id, msg, f"{text}\n{caption}" if caption else text)
        finally:
            with self.qlock:
                notes = self.voicing.get(chat_id, [])
                if start in notes:
                    notes.remove(start)
                if not notes:
                    self.voicing.pop(chat_id, None)

    def clean(self, text: str) -> str:
        """A message's words without the bot's @handle."""
        if self.handle:
            pattern = re.compile(rf"(?<![\w@])@{re.escape(self.handle)}(?!\w)", re.IGNORECASE)
            text = pattern.sub("", text)
        return re.sub(r"[ \t]{2,}", " ", text).strip()

    def addressed(self, msg: dict, text: str, private: bool) -> str | None:
        """The text without the bot's @handle when the bot should answer it, else None."""
        if private:
            return text.strip()
        if self.handle:
            pattern = re.compile(rf"(?<![\w@])@{re.escape(self.handle)}(?!\w)", re.IGNORECASE)
            if pattern.search(text):
                return re.sub(r"[ \t]{2,}", " ", pattern.sub("", text)).strip()
        if self.from_me(msg.get("reply_to_message")):
            return text.strip()
        return None

    # ---- commands

    def extras(self) -> dict:
        """What the help and the introduction say only when it is true of this instance."""
        voice = self.s.transcription.provider != "none"
        changes = llm.can_act(self.data)
        return {
            "handle": self.handle,
            "voice": self.t("help_voice") if voice else "",
            "changes": self.t("help_changes") if changes else "",
            "intro_voice": self.t("intro_voice") if voice else "",
            "intro_changes": self.t("intro_changes") if changes else "",
            "mentions": "" if self.mentions_ok else self.t("intro_mentions"),
        }

    def intro(self) -> str:
        x = self.extras()
        text = self.t(
            "intro",
            handle=x["handle"],
            voice=x["intro_voice"],
            changes=x["intro_changes"],
            mentions=x["mentions"],
        )
        return re.sub(r"\n{3,}", "\n\n", text).strip()

    def cmd_help(self, chat: int, msg: dict) -> None:
        x = self.extras()
        text = self.t("help", handle=x["handle"], voice=x["voice"], changes=x["changes"])
        self.send(chat, re.sub(r"\n{3,}", "\n\n", text).strip(), plain=True)

    def cmd_intro(self, chat: int, msg: dict) -> None:
        self.send(chat, self.intro(), plain=True)

    def cmd_chatid(self, chat: int, msg: dict) -> None:
        self.send(chat, self.t("chatid", id=chat), plain=True)

    def cmd_new(self, chat: int, msg: dict) -> None:
        conv = self.db.get_kv(f"conversation:{chat}")
        self.db.set_kv(f"conversation:{chat}", None)
        self.db.set_kv(f"offer:{chat}", None)
        self.db.set_kv(f"app:{chat}", None)
        self.db.clear_chat(chat)
        if conv:
            try:
                llm.forget(self.data, conv)
            except Exception as e:  # noqa: BLE001 - best effort; the chat already starts over
                log.warning("forget failed for chat %s: %s", chat, safe(e))
        self.send(chat, self.t("new_ok"), plain=True)

    def cmd_list(self, chat: int, msg: dict) -> None:
        try:
            rows = context.listing(self.cd, self.token)
        except CDError as e:
            log.warning("list failed: %s", safe(e))
            self.send(chat, self.t("instance_down"), plain=True)
            return
        tz = ZoneInfo(self.s.timezone)
        lines = []
        for n, uuid_, w in rows:
            if notifies(w, self.token) != chat:
                continue
            lines.append(
                self.t(
                    "list_item",
                    n=n,
                    title=w.get("title") or w.get("url"),
                    url=w.get("url"),
                    changed=context.when(w.get("last_changed"), tz),
                    error=(" ⚠️ " + str(w["last_error"])[:120]) if w.get("last_error") else "",
                )
            )
        text = (
            self.t("list_header", n=len(lines)) + "\n\n" + "\n\n".join(lines)
            if lines
            else self.t("list_empty")
        )
        self.send(chat, text, plain=True)

    # ---- one turn

    def settle(self, chat: int) -> None:
        """Wait until the chat's newest line that calls the bot is DEBOUNCE_S old and no voice note
        of the chat is still being transcribed (VOICE_WAIT_S at most from the note's start)."""
        while True:
            with self.qlock:
                newest = self.arrived.get(chat)
                notes = list(self.voicing.get(chat) or [])
            now = self.clock()
            wait = newest + self.debounce - now if newest is not None else 0
            if notes:
                wait = max(wait, min(notes) + self.voice_wait - now)
            if wait <= 0:
                return
            time.sleep(min(wait, SETTLE_STEP_S))

    def turn(self, chat: int) -> None:
        """One answer for every queued line of the chat. Lines that call the bot while it runs are
        handed to the provider (turn["incoming"]); those its answer does not cover stay queued for
        the next turn."""
        self.settle(chat)
        with self.qlock:
            rows = self.db.unsent(chat)
            if not any(_asked(r) for r in rows):
                return
            state = Turn(chat, {r["_id"] for r in rows})
            self.running[chat] = state
        reply = None
        stop = threading.Event()
        try:
            try:
                listing = context.listing(self.cd, self.token)
                facts = context.facts(self.cd, self.token, self.s, self.t, chat, listing)
                asked = [r["text"] for r in rows if _asked(r)]  # pages someone asked about
                files = context.files(self.cd, self.token, self.s, asked, listing)
            except CDError as e:
                log.warning("instance unreachable for chat %s: %s", chat, safe(e))
                self.send(chat, self.t("instance_down"), reply_to=_reply_to(rows), plain=True)
                return
            new = []
            note = self.db.get_kv(f"app:{chat}")
            if note:
                new.append({"role": "app", "content": note, "ts": _iso(_now())})
            new += [
                {"role": "user", "content": self.line(r, listing), "ts": _iso(r.get("ts"))}
                for r in rows
            ]
            turn = {
                "conversation": self.db.get_kv(f"conversation:{chat}"),
                "rules": context.rules(self.s, self.t),
                "context": facts,
                "files": files,
                "new": new,
                "history": self.db.history(chat, self.s.bot.history, self.s.bot.history_days),
                "incoming": self._incoming(state, listing),
            }
            threading.Thread(target=self._typing, args=(chat, stop), daemon=True).start()
            try:
                reply = llm.converse(self.data, turn)
            except Exception as e:  # noqa: BLE001 - the provider's text may quote anything
                log.warning("ai failed for chat %s: %s", chat, safe(e))
                self.send(chat, self.t("ai_down"), reply_to=_reply_to(rows), plain=True)
                return
        finally:
            stop.set()
            with self.qlock:
                state.closed = True  # a provider thread that outlives the turn takes nothing more
                self.running.pop(chat, None)
        took = state.ids | (set(state.pulled) & reply.covers)
        # read again: an edit during the turn changed a line's text, and the log keeps the last one
        covered = [r for r in self.db.unsent(chat) if r["_id"] in took]
        self.db.set_kv(f"conversation:{chat}", reply.conversation)
        if reply.kept:
            self.db.drop_unsent(chat, covered)
            self.db.set_kv(f"app:{chat}", None)
            for r in covered:
                self.db.add_chat(
                    chat,
                    "user",
                    r.get("who"),
                    self.line(r, listing, mark=False),
                    mid=None if r.get("edit") else r.get("msg"),
                    asked=_asked(r),
                    body=r.get("text"),
                )
            if note:
                self.db.add_chat(chat, "app", None, note)
        log.info(
            "turn for chat %s: %d lines, %d handed over, %d of them covered, kept=%s",
            chat,
            len(rows),
            len(state.pulled),
            len(took) - len(state.ids),
            reply.kept,
        )
        asked = [r for r in covered if _asked(r)] or [r for r in rows if _asked(r)]
        self.answer(chat, reply, asked[-1], listing, _reply_to(asked))

    def _incoming(self, state: Turn, listing: list):
        """turn["incoming"]: the chat's lines that called the bot since the last call, for the
        provider's thread. Once the turn is over it hands over nothing, so a late call steals
        nothing from the next turn."""

        def incoming() -> list[dict]:
            with self.qlock:
                if state.closed:
                    return []
                got = [
                    r
                    for r in self.db.unsent(state.chat)
                    if r["_id"] not in state.taken() and _asked(r)
                ]
                state.pulled.extend(r["_id"] for r in got)
            return [
                {
                    "id": r["_id"],
                    "role": "user",
                    "content": self.line(r, listing),
                    "ts": _iso(r.get("ts")),
                }
                for r in got
            ]

        return incoming

    def line(self, row: dict, listing: list, mark: bool = True) -> str:
        """The line as the AI reads it; a line that did not call the bot starts with `chat:`
        (mark False leaves that out: the log adds it when it is read, db.history)."""
        if row.get("edit"):  # the app's own note about an edit names who edited it
            return row["text"]
        who = row.get("who") or "someone"
        seen = "" if _asked(row) or not mark else "chat: "
        if row.get("ref"):
            n = next((n for n, u, _ in listing if u == row["ref"]), None)
            where = (
                f"replying to the notification of watch {n} ({row['ref']})"
                if n
                else f"replying to the notification of a watch that no longer exists ({row['ref']})"
            )
            return f"{seen}{who} ({where}): {row['text']}"
        return f"{seen}{who}: {row['text']}"

    # ---- edits

    def on_edit(self, msg: dict) -> None:
        """An edited message, handled by where it is (see the module's docstring). It never runs a
        command and is never a yes or a no."""
        chat, user = msg.get("chat") or {}, msg.get("from") or {}
        text = (msg.get("text") or msg.get("caption") or "").strip()
        if not text or text.startswith("/") or not self.allowed(chat, user):
            return
        chat_id, mid = int(chat["id"]), msg.get("message_id")
        private = chat.get("type") == "private"
        content = self.addressed(msg, text, private)
        calls = content is not None
        now = content if calls else self.clean(text)
        name = self.who(user)
        with self.qlock:
            state = self.running.get(chat_id)
            taken = state.taken() if state else set()
            queued = next(
                (
                    r
                    for r in reversed(self.db.unsent(chat_id))
                    if r.get("msg") == mid and not r.get("edit")
                ),
                None,
            )
            if queued is not None and queued["_id"] not in taken:
                # still waiting: the new text goes, and the debounce starts over
                if _same(queued["text"], now) and (_asked(queued) or not calls):
                    return
                queued.update(text=now, asked=_asked(queued) or calls, ts=_now())
                self.db.update_unsent(queued)
                if not queued["asked"]:
                    return
                self.arrived[chat_id] = self.clock()
        if queued is not None and queued["_id"] not in taken:
            self.worker(chat_id).kick()
            return
        if queued is not None:  # a line of the running turn
            before = queued["text"]
            if _same(before, now) and (_asked(queued) or not calls):
                return
            self.db.update_unsent({**queued, "text": now})
            if not _asked(queued):
                if calls:  # it rode along as context and now calls the bot: a new message
                    self.enqueue(chat_id, msg, now)
                return
            self.enqueue(
                chat_id,
                msg,
                f"[{name} edited a message of this turn. Reconsider your answer with the new "
                f"wording.]\nBefore: {before}\nNow: {now}",
                edit=True,
            )
            return
        row = self.db.find_chat(chat_id, mid) if mid is not None else None
        if row is None:  # never seen: a new message if it now calls the bot
            if calls:
                self.enqueue(chat_id, msg, now)
            return
        before = row.get("body") or ""
        logged = (
            row["text"].replace(before, now)
            if before and before in row["text"]
            else f"{row.get('who') or name}: {now}"
        )
        if row.get("asked") == 0:  # a message the bot only saw
            if not calls:
                if not _same(before, now):
                    self.db.edit_chat(row["id"], logged, now)
                return
            # the edit adds the mention: a new message to answer, logged again when answered
            self.db.delete_chat(row["id"])
            self.enqueue(chat_id, msg, now)
            return
        if _same(before, now):
            return
        self.db.edit_chat(row["id"], logged, now)
        said = (self.db.answer_after(chat_id, row["id"]) or "")[:ANSWER_CLIP]
        self.enqueue(
            chat_id,
            msg,
            f"[{name} edited a message you already answered. Check whether your answer still "
            f"holds with the new wording, and say what changes.]\nBefore: {before}\nNow: {now}"
            + (f"\nYour answer was: {said}" if said else ""),
            edit=True,
        )

    def answer(
        self, chat: int, reply: llm.Reply, last: dict, listing: list, reply_to: int | None
    ) -> None:
        """Show the answer (replying to reply_to, if any), run its actions and offer its deletes and
        proposals on behalf of `last`, the newest message it answers."""
        text = reply.text
        try:
            clean, acts = actions.extract(text)
            if acts:
                actions.resolve(acts, [(n, u) for n, u, _ in listing])
        except ActionError as e:
            log.warning("actions refused for chat %s: %s", chat, e)
            clean = actions.BLOCK.sub("", text).strip()
            self.db.add_chat(chat, "assistant", None, clean)
            self.db.set_kv(f"app:{chat}", f"Your actions block was refused and nothing ran: {e}")
            self.send(
                chat,
                (clean + "\n\n" if clean else "") + self.t("bad_actions"),
                reply_to=reply_to,
                plain=not clean,
            )
            return
        if clean:
            self.db.add_chat(chat, "assistant", None, clean)
            self.send(chat, render.to_html(clean), reply_to=reply_to)
            reply_to = None
        now = [a for a in acts or [] if a.op != "delete"]
        deletes = [a for a in acts or [] if a.op == "delete"]
        notes = []
        if now:
            rs = actions.apply(self.cd, now, self.s, self.token, chat, self.t)
            out = render.results(rs, self.t)
            self.send(chat, out, reply_to=reply_to, plain=True)
            notes.append("Results of your actions, as shown to the chat:\n" + out)
            for r in rs:
                if r.pending and r.uuid:
                    self.db.add_followup(
                        {"chat": chat, "uuid": r.uuid, "title": r.title or r.url, "since": _now()}
                    )
        if deletes:
            self.offer(chat, last, deletes, listing)
            notes.append(
                "The delete waits for the asker's confirmation; the chat will see the outcome."
            )
        if notes:
            self.db.set_kv(f"app:{chat}", "\n".join(notes))
        for p in reply.proposals:
            self.offer_proposal(chat, last, p)

    def _typing(self, chat: int, stop: threading.Event) -> None:
        while True:
            try:
                self.tg.typing(chat)
            except TelegramError:
                pass
            if stop.wait(TYPING_S):
                return

    # ---- delete offers

    def offer(self, chat: int, last: dict, deletes: list, listing: list) -> None:
        by_uuid = {u: w for _, u, w in listing}
        items = [
            {
                "uuid": a.watch,
                "title": by_uuid.get(a.watch, {}).get("title")
                or by_uuid.get(a.watch, {}).get("url")
                or a.watch,
            }
            for a in deletes
        ]
        offer = {
            "id": uuid.uuid4().hex[:8],
            "kind": "delete",
            "asker": last.get("user"),
            "by": [last.get("user")],
            "who": last.get("who"),
            "items": items,
            "expires": _now() + OFFER_TTL_S,
        }
        titles = ", ".join(f"«{i['title']}»" for i in items)
        markup = {
            "inline_keyboard": [
                [
                    {"text": self.t("button_yes"), "callback_data": f"del:{offer['id']}:y"},
                    {"text": self.t("button_no"), "callback_data": f"del:{offer['id']}:n"},
                ]
            ]
        }
        sent = self.send(
            chat,
            self.t("offer_delete", titles=titles, who=offer["who"]),
            reply_to=last.get("msg"),
            markup=markup,
            plain=True,
        )
        offer["msg"] = (sent or {}).get("message_id")
        self.db.set_kv(f"offer:{chat}", offer)

    # ---- proposals

    def offer_proposal(self, chat: int, last: dict, p: dict) -> None:
        """Something the provider would do outside the chat. Only `bot.users` may ask; a step with
        confirm_by none runs at once in the background, the others wait for the right yes."""
        reply_to = last.get("msg")
        if last.get("user") not in self.s.bot.users:
            self.send(chat, self.t("proposal_members_only"), reply_to=reply_to, plain=True)
            return
        pending = {
            "proposal": p,
            "chat": chat,
            "asker": last.get("user"),
            "who": last.get("who"),
            "reply_to": reply_to,
        }
        if p["confirm_by"] == "none":
            self.send(chat, p["summary"], reply_to=reply_to, plain=True)
            self.later(self.act, pending)
            return
        asker_only = p["confirm_by"] == "asker"
        offer = {
            "id": uuid.uuid4().hex[:8],
            "kind": "proposal",
            "asker": last.get("user"),
            "by": [last.get("user")] if asker_only else list(self.s.bot.users),
            "who": last.get("who"),
            "pending": pending,
            "expires": _now() + OFFER_TTL_S,
        }
        markup = {
            "inline_keyboard": [
                [
                    {"text": self.t("button_yes"), "callback_data": f"prop:{offer['id']}:y"},
                    {"text": self.t("button_no"), "callback_data": f"prop:{offer['id']}:n"},
                ]
            ]
        }
        key = "offer_proposal" if asker_only else "offer_proposal_users"
        sent = self.send(
            chat,
            self.t(key, summary=p["summary"], who=offer["who"]),
            reply_to=reply_to,
            markup=markup,
            plain=True,
        )
        offer["msg"] = (sent or {}).get("message_id")
        self.db.set_kv(f"offer:{chat}", offer)

    def act(self, pending: dict) -> None:
        """Have the provider carry out a proposal and post what came of it; follow-ups are offered
        on behalf of the same asker. While it runs it is written down, so a restart can say it was
        cut off instead of leaving the asker waiting."""
        chat, reply_to = pending["chat"], pending.get("reply_to")
        mark = {"id": uuid.uuid4().hex[:8], "chat": chat, "reply_to": reply_to}
        self._mark(mark, True)
        try:
            out = llm.act(self.data, pending["proposal"])
        except Exception as e:  # noqa: BLE001 - the provider's text may quote anything
            log.warning("a proposal failed for chat %s: %s", chat, safe(e))
            said = (
                self.t("proposal_unsupported")
                if isinstance(e, llm.NoAct)
                else self.t("proposal_failed", error=safe(e))
            )
            self.send(chat, said, reply_to=reply_to, plain=True)
            self.note(chat, "A proposal failed, as shown to the chat: " + said)
            return
        finally:
            self._mark(mark, False)
        if out.text:
            self.db.add_chat(chat, "assistant", None, out.text)
            self.send(chat, out.text, reply_to=reply_to, plain=True)
            self.note(chat, "Outcome of the proposal, as shown to the chat:\n" + out.text)
        for p in out.proposals:
            last = {"user": pending["asker"], "who": pending["who"], "msg": reply_to}
            self.offer_proposal(chat, last, p)

    def _mark(self, mark: dict, on: bool) -> None:
        with self._lock:
            marks = [m for m in self.db.get_kv(RUNNING) or [] if m.get("id") != mark["id"]]
            self.db.set_kv(RUNNING, ([*marks, mark] if on else marks) or None)

    def interrupted(self) -> None:
        """Once at start: tell each chat whose proposal was running when the bot stopped that it
        was cut off, so the asker knows to ask again."""
        for m in self.db.get_kv(RUNNING) or []:
            self.send(
                m["chat"], self.t("proposal_interrupted"), reply_to=m.get("reply_to"), plain=True
            )
        self.db.set_kv(RUNNING, None)

    def note(self, chat: int, text: str) -> None:
        """A line the AI gets with the next turn, after what is already waiting."""
        with self._lock:
            old = self.db.get_kv(f"app:{chat}")
            self.db.set_kv(f"app:{chat}", f"{old}\n{text}" if old else text)

    # ---- the open offer of a chat: a delete or a proposal, with Sí and No

    def open_offer(self, chat: int) -> dict | None:
        offer = self.db.get_kv(f"offer:{chat}")
        if offer and offer.get("expires", 0) >= _now():
            return offer
        if offer:
            self.db.set_kv(f"offer:{chat}", None)
        return None

    def on_callback(self, cq: dict) -> None:
        data = cq.get("data") or ""
        msg = cq.get("message") or {}
        chat = (msg.get("chat") or {}).get("id")
        user = cq.get("from") or {}
        parts = data.split(":")
        if chat is None or len(parts) != 3 or parts[0] not in ("del", "prop"):
            return
        chat = int(chat)
        offer = self.open_offer(chat)
        if not offer or offer.get("id") != parts[1]:
            self._cb(cq, self.t("offer_expired"))
            self._unmark(chat, msg.get("message_id"))
            return
        if user.get("id") not in self.answerers(offer):
            self._cb(cq, self.refusal(offer))
            return
        self._cb(cq)
        self._unmark(chat, offer.get("msg"))
        self.resolve_offer(chat, offer, parts[2] == "y")

    def answer_offer(self, chat: int, msg: dict, private: bool) -> bool:
        """A typed yes or no from the asker, replying to the offer or while the offer is the bot's
        latest message in the chat."""
        offer = self.open_offer(chat)
        if not offer or (msg.get("from") or {}).get("id") not in self.answerers(offer):
            return False
        word = _word(msg.get("text") or "")
        if word not in YES and word not in NO:
            return False
        replied = (msg.get("reply_to_message") or {}).get("message_id")
        if not (
            private
            or replied == offer.get("msg")
            or self.db.get_kv(f"sent:{chat}") == offer.get("msg")
        ):
            return False
        self._unmark(chat, offer.get("msg"))
        self.resolve_offer(chat, offer, word in YES, reply_to=msg.get("message_id"))
        return True

    @staticmethod
    def answerers(offer: dict) -> list:
        return offer.get("by") or [offer.get("asker")]

    def refusal(self, offer: dict) -> str:
        if len(self.answerers(offer)) == 1:
            return self.t("offer_only_asker", who=offer.get("who"))
        return self.t("offer_only_users")

    def resolve_offer(self, chat: int, offer: dict, yes: bool, reply_to: int | None = None) -> None:
        self.db.set_kv(f"offer:{chat}", None)
        if offer.get("kind") == "proposal":
            if not yes:
                self.send(chat, self.t("offer_proposal_no"), reply_to=reply_to, plain=True)
                self.note(chat, "The proposal was declined; nothing was done.")
                return
            pending = {**offer["pending"], "reply_to": reply_to or offer["pending"].get("reply_to")}
            self.later(self.act, pending)
            return
        if not yes:
            self.send(chat, self.t("offer_no"), reply_to=reply_to, plain=True)
            self.db.set_kv(f"app:{chat}", "The asker answered no: nothing was deleted.")
            return
        acts = [actions.Action("delete", i["uuid"]) for i in offer["items"]]
        rs = actions.apply(self.cd, acts, self.s, self.token, chat, self.t)
        out = render.results(rs, self.t)
        self.send(chat, out, reply_to=reply_to, plain=True)
        self.db.set_kv(f"app:{chat}", "The asker confirmed the delete. Result:\n" + out)

    def _cb(self, cq: dict, text: str | None = None) -> None:
        try:
            self.tg.answer_callback(cq.get("id"), text)
        except TelegramError as e:
            log.info("answerCallbackQuery failed: %s", safe(e))

    def _unmark(self, chat: int, message_id: int | None) -> None:
        if not message_id:
            return
        try:
            self.tg.edit_markup(chat, message_id, None)
        except TelegramError:
            pass

    # ---- pending first checks

    def _followups(self, stop: threading.Event) -> None:
        while not stop.wait(FOLLOWUP_EVERY_S):
            try:
                self.check_followups()
            except Exception as e:  # noqa: BLE001
                log.warning("follow-up pass failed: %s", safe(e))

    def check_followups(self) -> None:
        for f in self.db.followups():
            try:
                w = self.cd.watch(f["uuid"])
            except CDError as e:
                if "404" in str(e):
                    self.db.drop_followup(f)
                continue
            if int(w.get("last_checked") or 0) > f["since"]:
                seen = actions.seen(self.cd, w, self.s)
                key = "followup_error" if w.get("last_error") else "followup_ok"
                self.send(
                    f["chat"],
                    self.t(key, title=f["title"], seen=seen or self.t("result_seen_empty")),
                    plain=True,
                )
                self.db.drop_followup(f)
            elif _now() - f["since"] > FOLLOWUP_GIVE_UP_S:
                self.send(
                    f["chat"],
                    self.t("followup_late", title=f["title"], minutes=FOLLOWUP_GIVE_UP_S // 60),
                    plain=True,
                )
                self.db.drop_followup(f)

    # ---- sending

    def send(
        self,
        chat: int,
        text: str,
        reply_to: int | None = None,
        markup: dict | None = None,
        plain: bool = False,
    ) -> dict | None:
        """Every piece of `text`, HTML unless `plain`; the keyboard goes on the last piece."""
        pieces = list(render.chunks(text)) or [""]
        sent = None
        for i, piece in enumerate(pieces):
            last = i == len(pieces) - 1
            try:
                sent = self.tg.send(
                    chat,
                    html.escape(piece, quote=False) if plain else piece,
                    reply_to=reply_to if i == 0 else None,
                    markup=markup if last else None,
                )
            except TelegramError as e:
                log.warning("send to chat %s failed: %s", chat, safe(e))
                return sent
        if sent:
            self.db.set_kv(f"sent:{chat}", sent.get("message_id"))
        return sent


def run(data: Data, stop: threading.Event | None = None) -> None:
    cfg = data.settings
    token = data.secret(cfg.bot.token_env)
    if not token:
        raise SystemExit(f"{cfg.bot.token_env} is not set")
    key = data.secret(cfg.changedetection.api_key_env)
    if not key:
        raise SystemExit(f"{cfg.changedetection.api_key_env} is not set")
    bot = Bot(
        data, Telegram(token), CD(cfg.changedetection.url, key, cfg.changedetection.timeout_s)
    )
    bot.run(stop or threading.Event())


__all__ = ["Bot", "Worker", "dt", "run"]
