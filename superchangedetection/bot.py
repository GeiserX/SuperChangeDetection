"""The chat: who the bot answers, what each message becomes for the AI, and what the app does with
the answer.

- A private chat with one of `bot.users` is always answered. In the group (`bot.chat_id`) only a
  message that mentions the bot's @handle or replies to one of its messages is; a change
  notification comes from changedetection as the bot, so replying to one counts, and its last line
  `ref <uuid>` ties the reply to the watch.
- Each chat is one conversation for the AI (a plugin keeps it on its side; the other providers get
  the recent lines each time). Messages that arrive while a turn runs are queued in SQLite and go
  with the next turn, so a restart loses nothing.
- The AI's answer is shown as it is; its actions block is validated, creates, updates and rechecks
  run at once and the chat gets what each watch sees now; a delete is offered with two buttons and
  runs only after the person who asked says yes, within the hour.
- A create or update whose first check had not finished is followed up: the bot looks again every
  half minute and posts what the watch sees once the check ran.

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
}
YES = {"si", "yes", "vale", "ok", "s", "y", "dale"}
NO = {"no", "n", "nope"}


def _word(text: str) -> str:
    s = unicodedata.normalize("NFKD", text.strip().lower())
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"[^a-z]", "", s)


def _now() -> int:
    return int(time.time())


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
        self.workers: dict[int, Worker] = {}
        self._lock = threading.Lock()

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
        group = self.group()
        if group and not self.me.get("can_read_all_group_messages"):
            try:
                m = self.tg.get_chat_member(group, self.me.get("id"))
                if m.get("status") not in ("administrator", "creator"):
                    log.warning(
                        "privacy mode is on and the bot is not an admin of the group: it sees "
                        "commands and replies, not @mentions (make it an admin, or /setprivacy)"
                    )
            except TelegramError as e:
                log.info("could not check the bot's rights in the group: %s", safe(e))

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
        elif "callback_query" in u:
            self.on_callback(u["callback_query"])

    def on_message(self, msg: dict) -> None:
        chat, user = msg.get("chat") or {}, msg.get("from") or {}
        if msg.get("migrate_to_chat_id") and chat.get("id") == self.group():
            self.db.set_kv("group", int(msg["migrate_to_chat_id"]))
            log.warning("the group became a supergroup; update bot.chat_id when convenient")
            return
        text = msg.get("text") or msg.get("caption") or ""
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
            return
        ref = None
        if self.from_me(msg.get("reply_to_message")):
            found = REF.search(msg["reply_to_message"].get("text") or "")
            ref = found.group(1) if found else None
        self.db.push_unsent(
            chat_id,
            {
                "who": self.who(user),
                "user": user.get("id"),
                "text": content,
                "ref": ref,
                "msg": msg.get("message_id"),
                "ts": _now(),
            },
        )
        self.worker(chat_id).kick()

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

    def cmd_help(self, chat: int, msg: dict) -> None:
        self.send(chat, self.t("help", handle=self.handle), plain=True)

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

    def turn(self, chat: int) -> None:
        rows = self.db.unsent(chat)
        if not rows:
            return
        try:
            listing = context.listing(self.cd, self.token)
            facts = context.facts(self.cd, self.token, self.s, self.t, chat, listing)
            files = context.files(self.cd, self.token, self.s, [r["text"] for r in rows], listing)
        except CDError as e:
            log.warning("instance unreachable for chat %s: %s", chat, safe(e))
            self.send(chat, self.t("instance_down"), reply_to=rows[-1].get("msg"), plain=True)
            return
        new = []
        note = self.db.get_kv(f"app:{chat}")
        if note:
            new.append({"role": "app", "content": note})
        lines = [self.line(r, listing) for r in rows]
        new += [{"role": "user", "content": line} for line in lines]
        conv = self.db.get_kv(f"conversation:{chat}")
        turn = {
            "conversation": conv,
            "rules": context.rules(self.s, self.t),
            "context": facts,
            "files": files,
            "new": new,
            "history": self.db.history(chat, self.s.bot.history),
        }
        stop = threading.Event()
        threading.Thread(target=self._typing, args=(chat, stop), daemon=True).start()
        try:
            reply = llm.converse(self.data, turn)
        except Exception as e:  # noqa: BLE001 - the provider's text may quote anything
            log.warning("ai failed for chat %s: %s", chat, safe(e))
            self.send(chat, self.t("ai_down"), reply_to=rows[-1].get("msg"), plain=True)
            return
        finally:
            stop.set()
        self.db.set_kv(f"conversation:{chat}", reply.conversation)
        if reply.kept:
            self.db.drop_unsent(chat, rows)
            self.db.set_kv(f"app:{chat}", None)
            for r, line in zip(rows, lines, strict=True):
                self.db.add_chat(chat, "user", r.get("who"), line)
            if note:
                self.db.add_chat(chat, "app", None, note)
        log.info("turn for chat %s: %d lines, kept=%s", chat, len(rows), reply.kept)
        self.answer(chat, reply.text, rows[-1], listing)

    def line(self, row: dict, listing: list) -> str:
        who = row.get("who") or "someone"
        if row.get("ref"):
            n = next((n for n, u, _ in listing if u == row["ref"]), None)
            where = (
                f"replying to the notification of watch {n} ({row['ref']})"
                if n
                else f"replying to the notification of a watch that no longer exists ({row['ref']})"
            )
            return f"{who} ({where}): {row['text']}"
        return f"{who}: {row['text']}"

    def answer(self, chat: int, text: str, last: dict, listing: list) -> None:
        reply_to = last.get("msg")
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
        if not acts:
            return
        now = [a for a in acts if a.op != "delete"]
        deletes = [a for a in acts if a.op == "delete"]
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
            "asker": last.get("user"),
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
        if chat is None or len(parts) != 3 or parts[0] != "del":
            return
        chat = int(chat)
        offer = self.open_offer(chat)
        if not offer or offer.get("id") != parts[1]:
            self._cb(cq, self.t("offer_expired"))
            self._unmark(chat, msg.get("message_id"))
            return
        if user.get("id") != offer.get("asker"):
            self._cb(cq, self.t("offer_only_asker", who=offer.get("who")))
            return
        self._cb(cq)
        self._unmark(chat, offer.get("msg"))
        self.resolve_offer(chat, offer, parts[2] == "y")

    def answer_offer(self, chat: int, msg: dict, private: bool) -> bool:
        """A typed yes or no from the asker, replying to the offer or while the offer is the bot's
        latest message in the chat."""
        offer = self.open_offer(chat)
        if not offer or (msg.get("from") or {}).get("id") != offer.get("asker"):
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

    def resolve_offer(self, chat: int, offer: dict, yes: bool, reply_to: int | None = None) -> None:
        self.db.set_kv(f"offer:{chat}", None)
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
