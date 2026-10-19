"""The chat against a fake Telegram, the fake instance and a scripted AI: who gets an answer, what
the AI is sent, what the app does with its answer, delete confirmations and pending first checks."""

from __future__ import annotations

import json
import re

import httpx
import pytest
import yaml

from superchangedetection import bot as botmod
from superchangedetection import llm
from superchangedetection.bot import Bot
from superchangedetection.llm import Reply
from superchangedetection.model import Data
from superchangedetection.telegram import Telegram, TelegramError
from tests.conftest import TOKEN, U1, U2

GROUP = -5
ANA, LUIS, BOT_ID = 11, 22, 123
_MSG = [1000]


class FakeTelegram:
    """The Bot API as the bot uses it: records every call, answers getUpdates from a script."""

    def __init__(self):
        self.calls: list[tuple[str, dict]] = []
        self.updates: list[dict] = []
        self.fail_html = False
        self.fail_send = False  # every sendMessage refused, as for a user who never opened the bot
        self.files: dict[str, dict] = {}  # file_id -> the getFile answer, or {"error": description}

    def handler(self, request: httpx.Request) -> httpx.Response:
        method = request.url.path.rsplit("/", 1)[-1]
        assert f"/bot{TOKEN}/" in request.url.path
        params = json.loads(request.content) if request.content else {}
        self.calls.append((method, params))
        if "/file/bot" in request.url.path:  # the download of a file
            name = request.url.path.rsplit("/", 1)[-1]
            return httpx.Response(
                200, content=b"JPEG" + name.encode() if ".jpg" in name else b"OggS"
            )
        if method == "getMe":
            return self._ok(
                {"id": BOT_ID, "username": "watch_bot", "can_read_all_group_messages": False}
            )
        if method == "getUpdates":
            out, self.updates = self.updates, []
            return self._ok(out)
        if method == "sendMessage" and self.fail_send:
            return httpx.Response(
                403, json={"ok": False, "error_code": 403, "description": "Forbidden"}
            )
        if method == "sendMessage":
            if self.fail_html and params.get("parse_mode") == "HTML":
                return httpx.Response(
                    400,
                    json={"ok": False, "error_code": 400, "description": "can't parse entities"},
                )
            _MSG[0] += 1
            return self._ok({"message_id": _MSG[0], "chat": {"id": params["chat_id"]}})
        if method == "getChatMember":
            return self._ok({"status": "member"})
        if method == "getFile":
            got = self.files.get(params["file_id"], {"file_path": "voice/1.oga"})
            if "error" in got:
                return httpx.Response(
                    400, json={"ok": False, "error_code": 400, "description": got["error"]}
                )
            return self._ok({"file_id": params["file_id"], **got})
        return self._ok(True)

    @staticmethod
    def _ok(result):
        return httpx.Response(200, json={"ok": True, "result": result})

    def sent(self, method="sendMessage") -> list[dict]:
        return [p for m, p in self.calls if m == method]

    def texts(self) -> list[str]:
        return [p["text"] for p in self.sent()]


def message(text, chat=GROUP, user=ANA, name="Ana", reply_to=None, kind=None, mid=None):
    _MSG[0] += 1
    msg = {
        "message_id": mid or _MSG[0],
        "chat": {"id": chat, "type": kind or ("private" if chat > 0 else "group")},
        "from": {"id": user, "first_name": name},
        "text": text,
    }
    if reply_to:
        msg["reply_to_message"] = reply_to
    return msg


def from_bot(text, mid=500):
    return {"message_id": mid, "from": {"id": BOT_ID, "username": "watch_bot"}, "text": text}


@pytest.fixture
def world(tmp_path, cd, instance, monkeypatch):
    settings = {
        "locale": "es",
        "timezone": "Europe/Madrid",
        "bot": {"chat_id": GROUP, "users": [ANA], "history": 10},
        "changedetection": {"first_check_wait_s": 1},
        "llm": {"provider": "plugin", "plugin": "plugins/p.py"},
        "fetch": {"enabled": False},
    }
    (tmp_path / "settings.yaml").write_text(yaml.safe_dump(settings), encoding="utf-8")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    data = Data(tmp_path)
    ft = FakeTelegram()
    tg = Telegram(TOKEN, client=httpx.Client(transport=httpx.MockTransport(ft.handler)))
    b = Bot(data, tg, cd)
    b.start()
    b.debounce = 0  # a test runs each turn at once
    ai = {"answers": [], "turns": []}

    def converse(data_, turn):
        ai["turns"].append(turn)
        nxt = ai["answers"].pop(0) if ai["answers"] else "Vale."
        if isinstance(nxt, Exception):
            raise nxt
        return nxt if isinstance(nxt, Reply) else Reply(nxt, conversation="c1")

    monkeypatch.setattr(llm, "converse", converse)
    monkeypatch.setattr(botmod, "TYPING_S", 0.01)

    class NoWorker:  # the tests run each turn in their own thread, with turn()
        def kick(self):
            pass

    monkeypatch.setattr(b, "worker", lambda chat: NoWorker())
    return b, ft, ai, instance


def turn(b: Bot, msg: dict) -> None:
    """Deliver a message and run the chat's turn in this thread (no worker)."""
    b.on_message(msg)
    b.turn(msg["chat"]["id"])


# ---- who gets an answer


def test_private_chat_from_an_allowed_user_is_answered_and_a_stranger_is_not(world):
    b, ft, ai, _ = world
    turn(b, message("hola", chat=ANA, user=ANA))
    assert ft.texts()[-1] == "Vale." and len(ai["turns"]) == 1
    b.on_message(message("hola", chat=LUIS, user=LUIS, name="Luis"))
    assert not b.db.unsent(LUIS) and len(ai["turns"]) == 1


def test_group_answers_only_a_mention_or_a_reply_to_the_bot(world):
    b, ft, ai, _ = world
    b.on_message(message("vigila esto"))
    assert not b.db.unsent(GROUP)
    turn(b, message("@watch_bot vigila esto"))
    assert ai["turns"][0]["new"][-1]["content"] == "Ana: vigila esto"
    turn(b, message("y esto", reply_to=from_bot("Vale.")))
    assert ai["turns"][1]["new"][-1]["content"] == "Ana: y esto"
    assert ft.sent()[-1]["reply_parameters"]["message_id"]  # a quoted reply in the group
    b.on_message(message("@watch_bot hola", chat=-99))  # another group
    assert not b.db.unsent(-99)


def test_a_reply_to_a_notification_names_the_watch(world):
    b, _ft, ai, _ = world
    note = from_bot(f"🔔 Ha cambiado «A»\nhttps://a.example/x\n\n+price 12\n\nref {U1}")
    turn(b, message("avisa demasiado", reply_to=note))
    line = ai["turns"][0]["new"][-1]["content"]
    assert line.startswith(f"Ana (replying to the notification of watch 1 ({U1})): avisa demasiado")
    gone = from_bot("ref 99999999-9999-4999-8999-999999999999")
    turn(b, message("y esto", reply_to=gone))
    assert "no longer exists" in ai["turns"][1]["new"][-1]["content"]


# ---- what the AI gets


def test_the_turn_carries_rules_context_files_history_and_the_conversation(world):
    b, _ft, ai, _ = world
    turn(b, message("@watch_bot qué vigilas"))
    t = ai["turns"][0]
    assert t["conversation"] is None and "```actions" in t["rules"]
    assert "## 1. A" in t["context"] and f"snapshots/{U1}.txt" in t["files"]
    assert t["history"] == []
    turn(b, message("@watch_bot y el 2"))
    t2 = ai["turns"][1]
    assert t2["conversation"] == "c1"
    assert [m["role"] for m in t2["history"]] == ["user", "assistant"]
    assert t2["history"][0]["content"] == "Ana: qué vigilas"


def test_the_turn_names_its_chat_and_the_person_whose_line_started_it(world):
    b, _ft, ai, _ = world
    turn(b, message("@watch_bot hola", chat=ANA, user=ANA))
    assert ai["turns"][0]["chat"] == ANA and ai["turns"][0]["asker"] == ANA
    b.on_message(message("@watch_bot primero", user=LUIS, name="Luis"))
    b.on_message(message("@watch_bot segundo"))
    b.turn(GROUP)
    t = ai["turns"][1]
    assert [m["content"] for m in t["new"]] == ["Luis: primero", "Ana: segundo"]
    assert t["chat"] == GROUP and t["asker"] == LUIS  # a burst: the oldest line that calls the bot


def test_a_reply_to_someones_message_says_whose_what_and_when(world):
    b, _ft, ai, _ = world
    photo = {
        "message_id": 40,
        "from": {"id": LUIS, "first_name": "Luis"},
        "date": 1792000000,
        "photo": [{"file_id": "p1"}],
    }
    turn(b, message("@watch_bot ¿la viste?", reply_to=photo))
    line = ai["turns"][0]["new"][-1]["content"]
    assert line == "Ana (replying to Luis's photo of 2026-10-14T17:46:40+00:00): ¿la viste?"
    topic = {"message_id": 2, "from": {"id": LUIS}, "date": 1, "forum_topic_created": {}}
    turn(b, message("@watch_bot hola", reply_to=topic))  # a topic's first message is no reply
    assert ai["turns"][1]["new"][-1]["content"] == "Ana: hola"


def test_an_unkept_turn_sends_the_same_lines_again_and_a_failure_tells_the_chat(world):
    b, ft, ai, _ = world
    ai["answers"] = [Reply("Un momento.", conversation=None, kept=False)]
    turn(b, message("@watch_bot hola"))
    assert ft.texts()[-1] == "Un momento." and len(b.db.unsent(GROUP)) == 1
    ai["answers"] = [RuntimeError("boom")]
    turn(b, message("@watch_bot sigues ahí"))
    assert "no responde" in ft.texts()[-1] and len(b.db.unsent(GROUP)) == 2
    turn(b, message("@watch_bot ahora"))
    assert [m["content"] for m in ai["turns"][-1]["new"]] == [
        "Ana: hola",
        "Ana: sigues ahí",
        "Ana: ahora",
    ]
    assert not b.db.unsent(GROUP)


def busy(seconds):
    e = RuntimeError("no capacity")
    e.retry_after = seconds
    return e


def test_a_busy_provider_is_told_once_and_the_lines_are_answered_when_it_is_back(
    world, monkeypatch
):
    b, ft, ai, _ = world
    now = [1000.0]
    b.clock = lambda: now[0]
    woken, kicked = [], []
    monkeypatch.setattr(b, "later", lambda fn, *args: woken.append((fn, args)))
    monkeypatch.setattr(
        b, "worker", lambda chat: type("W", (), {"kick": lambda s: kicked.append(chat)})()
    )
    ai["answers"] = [busy(3960)]
    turn(b, message("@watch_bot primera"))
    assert ft.texts()[-1] == b.t("ai_busy", wait=b.t("busy_minutes", n=66))
    asked = len(ai["turns"])
    turn(b, message("@watch_bot segunda"))
    assert len(ai["turns"]) == asked  # it waits: no call while the provider is busy
    assert ft.texts().count(ft.texts()[-1]) == 1  # told once
    assert len(b.db.unsent(GROUP)) == 2  # queued in SQLite: a restart still answers them
    now[0] += 3960
    fn, args = woken[-1]
    kicked.clear()
    monkeypatch.setattr(botmod.time, "sleep", lambda s: None)
    fn(*args)
    assert kicked == [GROUP]  # nobody wrote again: the bot runs the turn by itself
    told = len(ft.texts())
    ai["answers"] = [busy(600)]
    b.turn(GROUP)
    assert len(ft.texts()) == told  # still the same spell: nothing more is said
    now[0] += 600
    ai["answers"] = ["Ya está."]
    b.turn(GROUP)
    assert [m["content"] for m in ai["turns"][-1]["new"]] == ["Ana: primera", "Ana: segunda"]
    assert ft.texts()[-1] == "Ya está." and not b.db.unsent(GROUP)
    ai["answers"] = [busy(600)]
    turn(b, message("@watch_bot otra"))
    assert ft.texts()[-1] == b.t("ai_busy", wait=b.t("busy_minutes", n=10))  # a new spell is told


def test_a_busy_spell_of_hours_says_hours_and_a_short_one_waits_a_minute(world, monkeypatch):
    b, ft, ai, _ = world
    monkeypatch.setattr(b, "later", lambda fn, *args: None)
    ai["answers"] = [busy(3 * 3600)]
    turn(b, message("@watch_bot hola", chat=ANA, user=ANA))
    assert ft.texts()[-1] == b.t("ai_busy", wait=b.t("busy_hours", n=3))
    ai["answers"] = [busy(5)]
    turn(b, message("@watch_bot hola"))
    assert ft.texts()[-1] == b.t("ai_busy", wait=b.t("busy_minute"))
    assert b.resting[GROUP] - b.clock() > botmod.BUSY_MIN_S - 5


def test_an_error_without_a_usable_retry_after_is_a_plain_failure(world, monkeypatch):
    b, ft, ai, _ = world
    monkeypatch.setattr(b, "later", lambda fn, *args: pytest.fail("no wake for a plain failure"))
    for bad in (True, 0, -5, "60", None, float("inf"), float("nan")):
        ai["answers"] = [busy(bad)]
        turn(b, message("@watch_bot hola"))
        assert "no responde" in ft.texts()[-1] and GROUP not in b.resting


# ---- what the app does with the answer


def test_a_create_runs_and_the_chat_sees_what_the_watch_sees(world):
    b, ft, ai, inst = world
    ai["answers"] = [
        (
            "Vigilo el precio cada 6 horas.\n\n```actions\n- op: create\n  url: https://c.example/\n"
            "  title: Precio\n  include_filters: ['.p']\n  time_between_check: {hours: 6}\n```"
        )
    ]
    turn(b, message("@watch_bot vigila https://c.example/"))
    texts = ft.texts()
    assert texts[-2] == "Vigilo el precio cada 6 horas."
    assert "Vigilo «Precio»" in texts[-1] and "new text" in texts[-1] and "dímelo" in texts[-1]
    w = next(w for w in inst.watches.values() if w.get("title") == "Precio")
    assert w["notification_urls"] == [f"tgram://{TOKEN}/{GROUP}/"]
    turn(b, message("@watch_bot gracias"))
    note = ai["turns"][-1]["new"][0]
    assert note["role"] == "app" and "Vigilo «Precio»" in note["content"]


def test_a_refused_block_touches_nothing_and_says_so(world):
    b, ft, ai, inst = world
    before = dict(inst.watches)
    ai["answers"] = ["Lo quito.\n\n```actions\n- op: delete\n  watch: 7\n```"]
    turn(b, message("@watch_bot borra el 7"))
    assert inst.watches == before
    assert ft.texts()[-1].startswith("Lo quito.") and "no he tocado nada" in ft.texts()[-1]
    turn(b, message("@watch_bot ?"))
    assert "refused" in ai["turns"][-1]["new"][0]["content"]


def test_a_delete_is_offered_and_only_the_asker_can_confirm(world):
    b, ft, ai, inst = world
    ai["answers"] = ["Borro A.\n\n```actions\n- op: delete\n  watch: 1\n```"]
    turn(b, message("@watch_bot borra el de A"))
    offer = ft.sent()[-1]
    assert "¿Dejo de vigilar «A»?" in offer["text"] and "Ana" in offer["text"]
    assert offer["text"].endswith("Si quieres otra cosa, responde a este mensaje.")
    buttons = offer["reply_markup"]["inline_keyboard"][0]
    assert [x["text"] for x in buttons] == ["Sí", "No"]
    assert U1 in inst.watches
    cq = {
        "id": "q1",
        "from": {"id": LUIS, "first_name": "Luis"},
        "message": {"message_id": 1, "chat": {"id": GROUP}},
        "data": buttons[0]["callback_data"],
    }
    b.on_callback(cq)
    assert U1 in inst.watches
    assert "Solo Ana" in ft.sent("answerCallbackQuery")[-1]["text"]
    b.on_callback({**cq, "from": {"id": ANA}})
    assert U1 not in inst.watches and "Borrado «A»" in ft.texts()[-1]
    b.on_callback({**cq, "from": {"id": ANA}})  # again: the offer is gone
    assert "caducó" in ft.sent("answerCallbackQuery")[-1]["text"]


def test_a_typed_no_from_the_asker_closes_the_offer(world):
    b, ft, ai, inst = world
    ai["answers"] = ["```actions\n- op: delete\n  watch: 1\n```"]
    turn(b, message("@watch_bot borra el de A"))
    offer_id = b.db.get_kv(f"offer:{GROUP}")["msg"]
    b.on_message(message("No", reply_to={"message_id": offer_id, "from": {"id": BOT_ID}}))
    assert "no borro nada" in ft.texts()[-1] and U1 in inst.watches
    assert b.db.get_kv(f"offer:{GROUP}") is None and not b.db.unsent(GROUP)


def test_an_expired_offer_is_a_normal_message_again(world, monkeypatch):
    b, _ft, ai, inst = world
    ai["answers"] = ["```actions\n- op: delete\n  watch: 1\n```"]
    turn(b, message("@watch_bot borra el de A"))
    monkeypatch.setattr(botmod, "_now", lambda: 10**10)
    b.on_message(message("@watch_bot sí"))
    assert b.db.unsent(GROUP)[0]["text"] == "sí" and U1 in inst.watches


# ---- pending first checks


def test_a_pending_first_check_is_followed_up(world, monkeypatch):
    b, ft, ai, inst = world
    monkeypatch.setattr(inst, "now", 0)
    uuid = "44444444-4444-4444-8444-444444444444"

    def slow_create(fields):
        inst.watches[uuid] = {"uuid": uuid, "last_checked": 0, **fields}
        return uuid

    monkeypatch.setattr(b.cd, "create", slow_create)
    ai["answers"] = ["```actions\n- op: create\n  url: https://c.example/\n  title: C\n```"]
    turn(b, message("@watch_bot vigila https://c.example/"))
    assert "aún no ha terminado" in ft.texts()[-1]
    assert len(b.db.followups()) == 1
    b.check_followups()
    assert len(b.db.followups()) == 1  # not checked yet
    inst.watches[uuid]["last_checked"] = 10**10
    inst.snapshots[uuid] = {"1": "now there"}
    b.check_followups()
    assert "primera comprobación de «C»" in ft.texts()[-1] and "now there" in ft.texts()[-1]
    assert not b.db.followups()


def test_a_first_check_that_never_ends_is_reported_once(world, monkeypatch):
    b, ft, _ai, inst = world
    b.db.add_followup({"chat": GROUP, "uuid": U2, "title": "B", "since": 10**9})
    inst.watches[U2]["last_checked"] = 10**9
    monkeypatch.setattr(botmod, "_now", lambda: 10**9 + 3600)
    b.check_followups()
    assert "sigue sin terminar" in ft.texts()[-1] and not b.db.followups()
    del inst.watches[U2]
    b.db.add_followup({"chat": GROUP, "uuid": U2, "title": "B", "since": 10**9})
    b.check_followups()  # a watch that is gone is dropped silently
    assert not b.db.followups()


# ---- commands and plumbing


def test_commands(world, instance):
    b, ft, _ai, _ = world
    b.on_message(message("/ayuda@watch_bot"))
    assert "/lista" in ft.texts()[-1] and "@watch_bot" in ft.texts()[-1]
    b.on_message(message("/lista"))
    assert "«A»" in ft.texts()[-1] and "«B»" not in ft.texts()[-1]
    b.on_message(message("/chatid"))
    assert str(GROUP) in ft.texts()[-1]
    turn(b, message("@watch_bot hola"))
    forgotten = []
    b.on_message(message("/nuevo"))
    assert "de cero" in ft.texts()[-1] and b.db.get_kv(f"conversation:{GROUP}") is None
    assert b.db.history(GROUP, 10) == [] and forgotten == []
    b.on_message(message("/lista@otherbot"))  # for another bot
    assert "de cero" in ft.texts()[-1]


def test_profile_is_synced_and_the_poll_keeps_its_offset(world):
    b, ft, _ai, _ = world
    cmds = ft.sent("setMyCommands")
    # the default scope first (what a client in any language shows), then the locale's
    assert [c.get("language_code") for c in cmds] == [None, "es"]
    assert all(c["commands"][0]["command"] == "lista" for c in cmds)
    descs = ft.sent("setMyDescription")
    assert [d.get("language_code") for d in descs] == [None, "es"]
    assert descs[0]["description"].startswith("Dime")
    ft.updates = [{"update_id": 7, "message": message("/chatid")}]
    assert b.poll_once() == 0
    assert b.db.get_kv("offset") == 8 and str(GROUP) in ft.texts()[-1]
    assert ft.sent("getUpdates")[-1]["timeout"] == botmod.POLL_S


def test_a_supergroup_migration_keeps_the_group(world):
    b, _ft, ai, _ = world
    b.on_message({**message("x"), "migrate_to_chat_id": -1005})
    assert b.group() == -1005
    turn(b, message("@watch_bot hola", chat=-1005, kind="supergroup"))
    assert ai["turns"]


def test_long_and_bad_html_answers_still_arrive(world):
    b, ft, ai, _ = world
    ai["answers"] = ["x" * 8000]
    turn(b, message("@watch_bot cuéntame"))
    assert len([t for t in ft.texts() if t.startswith("x")]) == 3
    ft.fail_html = True
    ai["answers"] = ["**hola** <b>"]
    turn(b, message("@watch_bot otra"))
    assert ft.texts()[-1] == "hola <b>"  # plain, after the HTML was refused


def test_token_never_appears_in_an_error(world):
    _b, _ft, _ai, _ = world
    real = (
        "1234567890:" + "AAE" + "x" * 32
    )  # shaped like a bot token, built here so no scanner trips
    err = TelegramError("sendMessage", 400, f"bad https://api.telegram.org/bot{real}/x")
    assert real not in str(err) and "<token>" in str(err)
    assert real not in botmod.safe(httpx.HTTPError(f"boom {real}"))


# ---- the plugin loader and the other providers


def test_plugin_converse_and_forget_are_loaded_from_the_data_dir(tmp_path, monkeypatch):
    (tmp_path / "plugins").mkdir()
    (tmp_path / "plugins" / "p.py").write_text(
        "seen = []\n"
        "def converse(turn, settings):\n"
        "    seen.append(turn['new'][-1]['content'])\n"
        "    return {'text': 'eco: ' + turn['new'][-1]['content'], 'conversation': 'k', 'kept': True}\n"
        "def forget(conversation, settings):\n"
        "    seen.append('forget ' + conversation)\n",
        encoding="utf-8",
    )
    (tmp_path / "settings.yaml").write_text(
        "llm:\n  provider: plugin\n  plugin: plugins/p.py\n  model: m\n", encoding="utf-8"
    )
    data = Data(tmp_path)
    r = llm.converse(data, {"new": [{"role": "user", "content": "hola"}]})
    assert r == Reply("eco: hola", "k", True)
    llm.forget(data, "k")
    bad = Data(tmp_path)
    bad.settings.llm.plugin = "../outside.py"
    with pytest.raises(llm.LLMError):
        llm.converse(bad, {"new": []})


def test_openai_and_anthropic_get_the_system_prompt_and_merged_messages(tmp_path, monkeypatch):
    posted = []

    def post(url, json, headers, timeout):
        posted.append((url, json))
        if "anthropic" in url:
            return httpx.Response(200, json={"content": [{"type": "text", "text": "ok"}]})
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    monkeypatch.setattr(httpx, "post", post)
    (tmp_path / "settings.yaml").write_text("llm:\n  provider: openai\n  model: m\n")
    turn = {
        "rules": "R",
        "context": "C",
        "files": {"pages/1.html": "<p>"},
        "history": [{"role": "user", "content": "Ana: a"}, {"role": "assistant", "content": "b"}],
        "new": [{"role": "app", "content": "did"}, {"role": "user", "content": "Ana: c"}],
    }
    assert llm.converse(Data(tmp_path), turn).text == "ok"
    body = posted[0][1]
    assert (
        body["messages"][0]["role"] == "system"
        and '<file path="pages/1.html">' in body["messages"][0]["content"]
    )
    assert [m["role"] for m in body["messages"][1:]] == ["user", "assistant", "user"]
    assert body["messages"][3]["content"] == "[app] did\nAna: c"
    (tmp_path / "settings.yaml").write_text("llm:\n  provider: anthropic\n  model: m\n")
    assert llm.converse(Data(tmp_path), turn).text == "ok"
    assert posted[1][1]["system"].startswith("R\n\nC")


def test_markdown_becomes_telegram_html():
    from superchangedetection.render import to_html

    out = to_html("# Título\n- **negrita** y `code` y [link](https://x.example/a_(b)) <b>")
    assert out == (
        '<b>Título</b>\n• <b>negrita</b> y <code>code</code> y <a href="https://x.example/a_(b)">link</a> &lt;b&gt;'
    )
    assert re.search(r"<pre>x\n</pre>|<pre>x</pre>", to_html("```py\nx\n```"))


# ---- voice notes


def voice_note(chat=GROUP, user=ANA, reply_to=None):
    msg = message("", chat=chat, user=user, reply_to=reply_to)
    del msg["text"]
    msg["voice"] = {"file_id": "f1", "duration": 3}
    return msg


@pytest.fixture
def inline(monkeypatch):
    """`later` runs the function in this thread, so a test sees the outcome at once."""

    def later(self, fn, *args):
        fn(*args)

    monkeypatch.setattr(Bot, "later", later)


def test_a_voice_note_is_always_answered_even_unaddressed_in_the_group(world, inline, monkeypatch):
    b, ft, ai, _ = world
    b.s.transcription.provider = "plugin"
    heard = []

    def transcribe(data_, audio, filename):
        heard.append((audio, filename))
        return "vigila la página de entradas"

    monkeypatch.setattr(llm, "transcribe", transcribe)
    b.on_message(voice_note())  # no mention, no reply: a note cannot carry either
    assert heard[0][1] == "1.oga" and ft.sent("getFile")[0]["file_id"] == "f1"
    echo = ft.sent()[-1]
    assert echo["text"] == "🎤 vigila la página de entradas"
    assert echo["reply_parameters"]["message_id"]
    assert b.db.unsent(GROUP)[0]["text"] == "vigila la página de entradas"
    b.turn(GROUP)
    assert ai["turns"][-1]["new"][-1]["content"] == "Ana: vigila la página de entradas"
    b.on_message(voice_note(user=LUIS, chat=LUIS))  # a stranger's note is ignored
    assert len(heard) == 1
    b.s.transcription.provider = "none"
    b.on_message(voice_note())  # no provider: nothing happens
    assert len(heard) == 1


def test_a_failed_transcription_tells_the_chat(world, inline, monkeypatch):
    b, ft, _ai, _ = world
    b.s.transcription.provider = "plugin"
    monkeypatch.setattr(llm, "transcribe", lambda *_: (_ for _ in ()).throw(RuntimeError("x")))
    b.on_message(voice_note())
    assert "nota de voz" in ft.texts()[-1] and not b.db.unsent(GROUP)


# ---- proposals


def test_a_proposal_with_no_confirmation_runs_in_the_background_and_reports(
    world, inline, monkeypatch
):
    b, ft, ai, _ = world
    acted = []

    def act(data_, proposal):
        acted.append(proposal["id"])
        return Reply("Hecho: fusionado.", proposals=[])

    monkeypatch.setattr(llm, "act", act)
    monkeypatch.setattr(llm, "can_act", lambda data_: True)
    p = {"id": "p1", "summary": "Me pongo con ello.", "confirm_by": "none", "what": "x"}
    ai["answers"] = [Reply("Vale, lo cambio.", conversation="c1", proposals=[p])]
    turn(b, message("@watch_bot añade un comando"))
    assert ft.texts()[-3:] == ["Vale, lo cambio.", "Me pongo con ello.", "Hecho: fusionado."]
    assert acted == ["p1"] and b.db.get_kv(botmod.RUNNING) is None
    turn(b, message("@watch_bot gracias"))
    assert "Hecho: fusionado." in ai["turns"][-1]["new"][0]["content"]
    # someone outside bot.users cannot ask for one
    ai["answers"] = [Reply("Vale.", conversation="c1", proposals=[p])]
    turn(b, message("@watch_bot otro", user=LUIS, name="Luis"))
    assert "quien me administra" in ft.texts()[-1] and acted == ["p1"]


def test_a_proposal_for_the_asker_waits_for_their_yes(world, inline, monkeypatch):
    b, ft, ai, _ = world
    acted = []
    monkeypatch.setattr(llm, "act", lambda d, p: (acted.append(p["id"]), Reply("Abierta."))[1])
    p = {"id": "p2", "summary": "Abriría el cambio.", "confirm_by": "asker"}
    ai["answers"] = [Reply("", conversation="c1", proposals=[p])]
    turn(b, message("@watch_bot cámbialo"))
    offer = ft.sent()[-1]
    assert offer["text"].startswith("Abriría el cambio.") and "Ana" in offer["text"]
    buttons = offer["reply_markup"]["inline_keyboard"][0]
    cq = {
        "id": "q",
        "from": {"id": LUIS},
        "message": {"message_id": offer_id(b), "chat": {"id": GROUP}},
        "data": buttons[0]["callback_data"],
    }
    b.on_callback(cq)
    assert acted == [] and "Solo Ana" in ft.sent("answerCallbackQuery")[-1]["text"]
    b.on_callback({**cq, "from": {"id": ANA}})
    assert acted == ["p2"] and ft.texts()[-1] == "Abierta."
    # a typed no closes one that any user may answer
    ai["answers"] = [Reply("", conversation="c1", proposals=[{**p, "confirm_by": "admins"}])]
    turn(b, message("@watch_bot otra vez"))
    assert "quien me administra" in ft.texts()[-1]
    b.on_message(message("no", reply_to={"message_id": offer_id(b), "from": {"id": BOT_ID}}))
    assert "no lo hago" in ft.texts()[-1] and acted == ["p2"]


def offer_id(b: Bot) -> int:
    return b.db.get_kv(f"offer:{GROUP}")["msg"]


def test_a_failed_proposal_and_an_interrupted_one_are_reported(world, inline, monkeypatch):
    b, ft, _ai, _ = world
    monkeypatch.setattr(llm, "act", lambda d, p: (_ for _ in ()).throw(RuntimeError("boom")))
    b.act({"proposal": {"id": "x"}, "chat": GROUP, "asker": ANA, "who": "Ana", "reply_to": 1})
    assert "No ha salido bien" in ft.texts()[-1]
    b.db.set_kv(botmod.RUNNING, [{"id": "m", "chat": GROUP, "reply_to": 1}])
    b.start()
    assert "Me reinicié" in ft.texts()[-1] and b.db.get_kv(botmod.RUNNING) is None


# ---- the version announcement


def announced(ft) -> list[tuple[int, str]]:
    return [(p["chat_id"], p["text"]) for p in ft.sent() if "versión" in p["text"]]


def test_the_first_start_announces_the_version_to_each_admin_in_private(world):
    b, ft, _ai, _ = world
    assert announced(ft) == [(ANA, f"Nueva versión en marcha: {botmod.__version__}.")]
    assert b.db.get_kv("version") == botmod.__version__


def test_a_restart_on_the_same_version_says_nothing(world):
    b, ft, _ai, _ = world
    ft.calls.clear()
    b.start()
    assert announced(ft) == []


def test_a_new_version_says_which_one_it_replaces(world, monkeypatch):
    b, ft, _ai, _ = world
    old = botmod.__version__
    ft.calls.clear()
    monkeypatch.setattr(botmod, "__version__", "9.9.9")
    b.start()
    assert announced(ft) == [(ANA, f"Nueva versión en marcha: 9.9.9 (antes {old}).")]
    assert b.db.get_kv("version") == "9.9.9"


def test_a_refused_announcement_is_not_stored_and_goes_out_at_the_next_start(world, monkeypatch):
    b, ft, _ai, _ = world
    monkeypatch.setattr(botmod, "__version__", "9.9.9")
    ft.fail_send = True
    b.start()
    assert b.db.get_kv("version") != "9.9.9"
    ft.fail_send = False
    ft.calls.clear()
    b.start()
    assert [chat for chat, _ in announced(ft)] == [ANA]
    assert b.db.get_kv("version") == "9.9.9"


def test_with_no_admins_the_group_gets_the_announcement(world):
    b, ft, _ai, _ = world
    b.s.bot.users = []
    b.db.set_kv("version", None)
    ft.calls.clear()
    b.start()
    assert [chat for chat, _ in announced(ft)] == [GROUP]


# ---- the template and the introduction


def test_the_notification_template_is_synced_onto_the_bots_watches_at_start(world, instance):
    b, _ft, _ai, inst = world
    inst.watches[U1]["notification_body"] = "old"
    inst.watches[U2]["notification_body"] = "old"  # not the bot's: left alone
    inst.calls.clear()  # start() synced once already
    b.sync_notifications()
    assert "Ver el cambio: {{diff_url}}" in inst.watches[U1]["notification_body"]
    assert "{{diff}}" not in inst.watches[U1]["notification_body"]
    assert inst.watches[U2]["notification_body"] == "old"
    puts = [c for c in inst.calls if c[0] == "PUT"]
    assert len(puts) == 1
    inst.calls.clear()
    b.sync_notifications()  # nothing to do the second time
    assert not [c for c in inst.calls if c[0] == "PUT"]


def test_the_intro_says_what_is_true_of_this_instance(world, monkeypatch):
    b, ft, _ai, _ = world
    b.on_message(message("/presentacion"))
    text = ft.texts()[-1]
    assert text.startswith("¡Hola! Soy @watch_bot") and "\n\n\n" not in text
    assert "administrador" in text  # privacy on and not an admin: mentions do not arrive
    assert "notas de voz" not in text and "otra forma" not in text
    b.s.transcription.provider = "plugin"
    monkeypatch.setattr(llm, "can_act", lambda data_: True)
    b.mentions_ok = True
    assert "notas de voz" in b.intro() and "lo cambio yo" in b.intro()
    assert "administrador" not in b.intro()
    b.on_message(message("/ayuda"))
    assert "lo cambio yo" in ft.texts()[-1] and "/presentacion" in ft.texts()[-1]


def test_plugin_act_and_transcribe_are_loaded_and_missing_act_is_no_act(tmp_path, monkeypatch):
    (tmp_path / "plugins").mkdir()
    (tmp_path / "plugins" / "p.py").write_text(
        "def converse(turn, settings):\n"
        "    assert turn['proposals'] is True\n"
        "    return {'text': 'ok', 'proposals': [{'id': 'a', 'summary': ' s ', 'confirm_by': 'none'},"
        " {'id': 'b', 'summary': 's', 'confirm_by': 'boss'}, 'junk'], 'kept': True}\n"
        "def act(proposal, settings):\n"
        "    return {'text': 'done ' + proposal['id'], 'proposals': []}\n"
        "def transcribe(audio, filename, settings):\n"
        "    return f'{len(audio)} {filename} {settings[\"language\"]}'\n",
        encoding="utf-8",
    )
    (tmp_path / "settings.yaml").write_text(
        "llm:\n  provider: plugin\n  plugin: plugins/p.py\n"
        "transcription:\n  provider: plugin\n  plugin: plugins/p.py\n  language: es\n",
        encoding="utf-8",
    )
    data = Data(tmp_path)
    assert llm.can_act(data)
    r = llm.converse(data, {"new": []})
    assert r.proposals == [{"id": "a", "summary": "s", "confirm_by": "none"}]
    assert llm.act(data, {"id": "a"}).text == "done a"
    assert llm.transcribe(data, b"abc", "v.oga") == "3 v.oga es"
    (tmp_path / "plugins" / "q.py").write_text("def converse(t, s):\n    return 'x'\n")
    (tmp_path / "settings.yaml").write_text("llm:\n  provider: plugin\n  plugin: plugins/q.py\n")
    data = Data(tmp_path)
    assert not llm.can_act(data) and llm.converse(data, {"new": []}).proposals == []
    with pytest.raises(llm.NoAct):
        llm.act(data, {"id": "a"})
    with pytest.raises(llm.LLMError):
        llm.transcribe(data, b"", "v.oga")


def test_openai_transcription_posts_the_audio(tmp_path, monkeypatch):
    posted = []

    def post(url, data, files, headers, timeout):
        posted.append((url, data, files, headers))
        return httpx.Response(200, json={"text": " hola "})

    monkeypatch.setattr(httpx, "post", post)
    monkeypatch.setenv("STT_API_KEY", "k")
    (tmp_path / "settings.yaml").write_text(
        "transcription:\n  provider: openai\n  language: es\n", encoding="utf-8"
    )
    assert llm.transcribe(Data(tmp_path), b"abc", "v.oga") == "hola"
    url, form, files, headers = posted[0]
    assert url.endswith("/audio/transcriptions") and form["language"] == "es"
    assert files["file"] == ("v.oga", b"abc") and headers["Authorization"] == "Bearer k"


# ---- one answer per burst, messages during a turn, edits


def edit(msg, text):
    return {**msg, "text": text, "edit_date": 1}


def replies(ft) -> list:
    return [(p["text"], (p.get("reply_parameters") or {}).get("message_id")) for p in ft.sent()]


def test_a_burst_is_one_turn_and_one_plain_answer(world):
    b, ft, ai, _ = world
    m1, m2 = message("@watch_bot vigila https://a.example/x"), message("@watch_bot cada hora")
    b.on_message(m1)
    b.on_message(m2)
    b.turn(GROUP)
    assert len(ai["turns"]) == 1
    assert [m["content"] for m in ai["turns"][0]["new"]] == [
        "Ana: vigila https://a.example/x",
        "Ana: cada hora",
    ]
    assert all(m["ts"] for m in ai["turns"][0]["new"])
    assert replies(ft)[-1] == ("Vale.", None)  # several messages: no reply to one of them
    assert not b.db.unsent(GROUP)
    b.turn(GROUP)  # nothing left: no second answer
    assert len(ai["turns"]) == 1


def test_a_single_message_gets_a_reply_to_it(world):
    b, ft, _ai, _ = world
    m = message("@watch_bot qué vigilas")
    turn(b, m)
    assert replies(ft)[-1] == ("Vale.", m["message_id"])


def test_the_turn_waits_for_the_newest_message_and_for_a_voice_note(world, monkeypatch):
    b, *_ = world
    clock = [100.0]
    b.clock = lambda: clock[0]
    slept = []

    def sleep(s):
        slept.append(s)
        clock[0] += s

    monkeypatch.setattr(botmod.time, "sleep", sleep)
    b.debounce, b.voice_wait = 2.5, 30
    b.arrived[GROUP] = 100.0
    b.settle(GROUP)
    assert clock[0] == pytest.approx(102.5)
    b.voicing[GROUP] = [101.0]  # a note started before; still transcribing
    b.settle(GROUP)
    assert clock[0] == pytest.approx(131.0)  # 30 s from the note's start, no more
    b.voicing.clear()
    b.arrived[GROUP] = clock[0] - 10  # an old message: no wait at all
    n = len(slept)
    b.settle(GROUP)
    assert len(slept) == n


def test_a_message_during_the_turn_joins_it_and_the_answer_covers_both(world, monkeypatch):
    b, ft, ai, _ = world
    m1, m2 = message("@watch_bot vigila https://a.example/x"), message("@watch_bot y cada hora")
    handed = []

    def converse(data_, t):
        ai["turns"].append(t)
        b.on_message(m2)  # arrives while the answer is being written
        got = t["incoming"]()
        handed.extend(got)
        assert t["incoming"]() == []  # handed over once
        return Reply("Hecho, cada hora.", conversation="c1", covers={g["id"] for g in got})

    monkeypatch.setattr(llm, "converse", converse)
    b.on_message(m1)
    b.turn(GROUP)
    assert [h["content"] for h in handed] == ["Ana: y cada hora"] and handed[0]["ts"]
    assert len(ai["turns"]) == 1 and replies(ft)[-1] == ("Hecho, cada hora.", None)
    assert not b.db.unsent(GROUP)
    logged = [r["text"] for r in b.db.chat_rows(GROUP, 10)]
    assert logged == ["Ana: vigila https://a.example/x", "Ana: y cada hora", "Hecho, cada hora."]
    assert t_incoming_after_close(b, ai) == []


def t_incoming_after_close(b, ai):
    b.on_message(message("@watch_bot otra"))
    return ai["turns"][-1]["incoming"]()  # the turn is over: a late call takes nothing


def test_a_message_the_answer_did_not_cover_goes_with_the_next_turn(world, monkeypatch):
    b, ft, ai, _ = world
    m2 = message("@watch_bot y otra cosa")
    script = []

    def converse(data_, t):
        ai["turns"].append(t)
        if not script:
            script.append(1)
            b.on_message(m2)
            t["incoming"]()
            return Reply("Primera.", conversation="c1")  # covers nothing it was handed
        return Reply("Segunda.", conversation="c1")

    monkeypatch.setattr(llm, "converse", converse)
    m1 = message("@watch_bot hola")
    turn(b, m1)
    assert replies(ft)[-1] == ("Primera.", m1["message_id"])
    assert [r["text"] for r in b.db.unsent(GROUP)] == ["y otra cosa"]
    b.turn(GROUP)
    assert ai["turns"][-1]["new"][-1]["content"] == "Ana: y otra cosa"
    assert replies(ft)[-1] == ("Segunda.", m2["message_id"]) and not b.db.unsent(GROUP)


def test_a_turn_the_provider_did_not_keep_sends_its_handed_lines_again(world, monkeypatch):
    b, _ft, ai, _ = world
    m2 = message("@watch_bot segunda")

    def converse(data_, t):
        ai["turns"].append(t)
        b.on_message(m2)
        got = t["incoming"]()
        return Reply("x", conversation=None, kept=False, covers={g["id"] for g in got})

    monkeypatch.setattr(llm, "converse", converse)
    turn(b, message("@watch_bot primera"))
    assert [r["text"] for r in b.db.unsent(GROUP)] == ["primera", "segunda"]


def test_an_edit_of_a_waiting_message_sends_the_new_text(world):
    b, _ft, ai, _ = world
    m = message("@watch_bot vigila https://a.example/x")
    b.on_message(m)
    b.on_edit(edit(m, "@watch_bot vigila https://a.example/y"))
    b.turn(GROUP)
    assert [x["content"] for x in ai["turns"][0]["new"]] == ["Ana: vigila https://a.example/y"]
    assert b.db.find_chat(GROUP, m["message_id"])["body"] == "vigila https://a.example/y"


def test_an_edit_during_the_turn_reaches_it_as_a_correction(world, monkeypatch):
    b, ft, ai, _ = world
    m = message("@watch_bot vigila https://a.example/x cada día")
    handed = []

    def converse(data_, t):
        ai["turns"].append(t)
        b.on_edit(edit(m, "@watch_bot vigila https://a.example/x cada hora"))
        got = t["incoming"]()
        handed.extend(got)
        return Reply("Cada hora, entonces.", conversation="c1", covers={g["id"] for g in got})

    monkeypatch.setattr(llm, "converse", converse)
    turn(b, m)
    assert handed[0]["content"] == (
        "[Ana edited a message of this turn. Reconsider your answer with the new wording.]\n"
        "Before: vigila https://a.example/x cada día\nNow: vigila https://a.example/x cada hora"
    )
    assert replies(ft)[-1] == ("Cada hora, entonces.", m["message_id"])  # one message answered
    assert len(ai["turns"]) == 1 and not b.db.unsent(GROUP)
    row = b.db.find_chat(GROUP, m["message_id"])  # the log keeps the final wording
    assert row["body"] == "vigila https://a.example/x cada hora"


def test_an_edit_of_an_answered_message_is_a_new_turn_with_the_diff(world):
    b, ft, ai, _ = world
    ai["answers"] = [Reply("Vigilo el precio.", conversation="c1")]
    m = message("@watch_bot vigila el precio")
    turn(b, m)
    b.on_edit(edit(m, "@watch_bot vigila el stock"))
    b.turn(GROUP)
    assert ai["turns"][-1]["new"][-1]["content"] == (
        "[Ana edited a message you already answered. Check whether your answer still holds with "
        "the new wording, and say what changes.]\nBefore: vigila el precio\nNow: vigila el stock\n"
        "Your answer was: Vigilo el precio."
    )
    assert replies(ft)[-1] == ("Vale.", m["message_id"])
    assert b.db.find_chat(GROUP, m["message_id"])["text"] == "Ana: vigila el stock"


def test_edits_that_change_nothing_run_commands_or_answer_offers_do_nothing(world):
    b, ft, ai, _ = world
    m = message("@watch_bot vigila el precio")
    turn(b, m)
    n_turns, n_sent = len(ai["turns"]), len(ft.sent())
    b.on_edit(edit(m, "@watch_bot   vigila el  precio"))  # spacing only
    b.on_edit(edit(m, "/lista"))
    b.db.set_kv(
        f"offer:{GROUP}",
        {"id": "o1", "kind": "delete", "asker": ANA, "by": [ANA], "items": [], "expires": 2**40},
    )
    b.on_edit(edit(message("@watch_bot sí", chat=ANA, user=ANA), "sí"))
    assert b.db.get_kv(f"offer:{GROUP}")  # an edit is never a yes
    assert not [r for r in b.db.unsent(GROUP)] and len(ft.sent()) == n_sent
    assert len(ai["turns"]) == n_turns


def test_an_edit_that_adds_the_mention_is_answered(world):
    b, ft, ai, _ = world
    m = message("vigila https://a.example/x")  # not for the bot, and history_scope is "asked"
    b.on_message(m)
    assert not b.db.unsent(GROUP)
    b.on_edit(edit(m, "vigila https://a.example/x por favor"))  # still not for the bot
    assert not b.db.unsent(GROUP)
    b.on_edit(edit(m, "@watch_bot vigila https://a.example/x"))
    b.turn(GROUP)
    assert ai["turns"][-1]["new"][-1]["content"] == "Ana: vigila https://a.example/x"
    assert replies(ft)[-1] == ("Vale.", m["message_id"])


def test_with_history_scope_all_the_group_is_context_and_its_edits_count(world):
    b, ft, ai, _ = world
    b.s.bot.history_scope = "all"
    seen = message("mañana miro lo de las entradas", user=LUIS, name="Luis")
    b.on_message(seen)
    b.turn(GROUP)
    assert ai["turns"] == []  # nobody called the bot: no turn
    b.on_edit(edit(seen, "pasado mañana miro lo de las entradas"))
    m = message("@watch_bot vigila https://a.example/x")
    b.on_message(m)
    b.turn(GROUP)
    assert [x["content"] for x in ai["turns"][0]["new"]] == [
        "chat: Luis: pasado mañana miro lo de las entradas",
        "Ana: vigila https://a.example/x",
    ]
    assert replies(ft)[-1] == ("Vale.", m["message_id"])  # only one message called the bot
    b.on_edit(edit(seen, "pasado mañana miro lo de las entradas, ya"))  # seen, still unaddressed
    assert b.db.find_chat(GROUP, seen["message_id"])["body"].endswith(", ya") and not b.db.unsent(
        GROUP
    )
    hist = b.db.history(GROUP, 10)
    assert hist[0]["content"] == "chat: Luis: pasado mañana miro lo de las entradas, ya"
    b.on_edit(edit(seen, "@watch_bot pasado mañana miro lo de las entradas, ya"))
    b.turn(GROUP)
    assert ai["turns"][-1]["new"][-1]["content"] == (
        "Luis: pasado mañana miro lo de las entradas, ya"
    )
    assert replies(ft)[-1] == ("Vale.", seen["message_id"])


def test_a_voice_note_holds_the_turn_until_heard_and_keeps_its_caption(world, monkeypatch):
    b, ft, ai, _ = world
    b.s.transcription.provider = "plugin"
    monkeypatch.setattr(llm, "transcribe", lambda *_: "vigila las entradas")
    later = []
    monkeypatch.setattr(b, "later", lambda fn, *args: later.append((fn, args)))
    note = voice_note()
    note["caption"] = "@watch_bot mira esto"
    b.on_message(note)
    assert len(b.voicing[GROUP]) == 1  # being transcribed: the chat's next turn waits for it
    fn, args = later.pop()
    fn(*args)
    assert GROUP not in b.voicing and ft.texts()[-1] == "🎤 vigila las entradas"
    b.turn(GROUP)
    assert ai["turns"][-1]["new"][-1]["content"] == "Ana: vigila las entradas\nmira esto"
    audio = message("", chat=ANA, user=ANA)
    del audio["text"]
    audio["audio"] = {"file_id": "f2", "duration": 9}
    b.on_message(audio)  # an audio file in a private chat is heard too
    fn, args = later.pop()
    fn(*args)
    assert [r["text"] for r in b.db.unsent(ANA)] == ["vigila las entradas"] and not b.voicing
    b.on_edit({**note, "caption": "@watch_bot mira esto otro", "edit_date": 1})
    assert [r["text"] for r in b.db.unsent(ANA)] == ["vigila las entradas"]  # the transcript stays
    assert not b.db.unsent(GROUP)  # and a caption edit of the answered note starts no new turn


def test_the_bot_asks_telegram_for_edits(world):
    b, ft, _ai, _ = world
    b.poll_once()
    assert "edited_message" in ft.sent("getUpdates")[-1]["allowed_updates"]


def test_an_old_database_gains_the_new_columns(tmp_path):
    import sqlite3

    from superchangedetection.db import DB

    path = tmp_path / "bot.sqlite"
    c = sqlite3.connect(path)
    c.execute(
        "CREATE TABLE chat (id INTEGER PRIMARY KEY AUTOINCREMENT, chat INTEGER NOT NULL, "
        "ts TEXT NOT NULL, role TEXT NOT NULL, who TEXT, text TEXT NOT NULL)"
    )
    c.execute(
        "INSERT INTO chat (chat, ts, role, who, text) VALUES (1, '2026-01-01', 'user', 'A', 'x')"
    )
    c.commit()
    c.close()
    db = DB(path)
    assert db.history(1, 5) == [{"role": "user", "content": "x", "ts": "2026-01-01"}]
    db.add_chat(1, "user", "A", "A: y", mid=7, asked=True, body="y")
    assert db.find_chat(1, 7)["body"] == "y"


def test_history_days_brings_every_message_of_those_days(tmp_path):
    from superchangedetection.db import DB

    db = DB(tmp_path / "bot.sqlite")
    for n in range(6):
        db.add_chat(1, "user", "A", f"A: {n}")
    assert [h["content"] for h in db.history(1, 2)] == ["A: 4", "A: 5"]
    assert len(db.history(1, 2, days=14)) == 6


def test_on_the_real_worker_a_burst_and_a_late_message_get_one_answer(world, monkeypatch):
    import threading
    import time

    b, ft, ai, _ = world
    b.__dict__.pop("worker")  # the real per-chat thread, not the tests' inline turns
    b.debounce = 0.3
    started, late_sent = threading.Event(), threading.Event()

    def converse(data_, t):
        ai["turns"].append(t)
        started.set()
        assert late_sent.wait(5)
        got = []
        for _ in range(50):  # the plugin looks for new lines while it works
            got += t["incoming"]()
            if got:
                break
            time.sleep(0.05)
        return Reply("Todo hecho.", conversation="c1", covers={g["id"] for g in got})

    monkeypatch.setattr(llm, "converse", converse)
    b.on_message(message("@watch_bot uno"))
    time.sleep(0.1)
    b.on_message(message("@watch_bot dos"))  # inside the debounce: same turn
    assert started.wait(5)
    b.on_message(message("@watch_bot tres"))  # while the answer is written: joins it
    late_sent.set()
    for _ in range(100):
        if "Todo hecho." in ft.texts() and not b.db.unsent(GROUP):
            break
        time.sleep(0.05)
    time.sleep(0.5)  # a second turn, if any, would have started by now
    assert len(ai["turns"]) == 1
    assert [m["content"] for m in ai["turns"][0]["new"]] == ["Ana: uno", "Ana: dos"]
    assert ft.texts().count("Todo hecho.") == 1 and not b.db.unsent(GROUP)


# ---- photos and files


def photo(caption=None, chat=GROUP, user=ANA, sizes=((90, 1_000, "s"), (1280, 200_000, "b"))):
    msg = message("", chat=chat, user=user)
    del msg["text"]
    msg["photo"] = [{"file_id": f, "width": w, "file_size": n} for w, n, f in sizes]
    if caption is not None:
        msg["caption"] = caption
    return msg


def document(name, mime, size=50_000, file_id="d1", chat=GROUP):
    msg = message("", chat=chat)
    del msg["text"]
    msg["document"] = {"file_id": file_id, "file_name": name, "mime_type": mime, "file_size": size}
    return msg


def test_a_photo_reaches_the_ai_with_its_caption_even_unaddressed_in_the_group(world):
    b, ft, ai, _ = world
    ft.files["b"] = {"file_path": "photos/file_7.jpg", "file_size": 200_000}
    m = photo("@watch_bot vigila el precio de esto")
    turn(b, m)
    name = f"inbox/{m['message_id']}-photo.jpg"
    assert ft.sent("getFile")[-1]["file_id"] == "b"  # the largest size
    t = ai["turns"][-1]
    assert t["new"][-1]["content"] == f"Ana: [photo: {name}] vigila el precio de esto"
    assert t["attachments"] == [
        {"name": name, "mime": "image/jpeg", "data": b"JPEGfile_7.jpg", "from": "Ana"}
    ]
    assert replies(ft)[-1] == ("Vale.", m["message_id"]) and not b.db.unsent(GROUP)
    bare = photo()  # no caption, no mention: answered all the same, like a voice note
    turn(b, bare)
    assert (
        ai["turns"][-1]["new"][-1]["content"]
        == f"Ana: [photo: inbox/{bare['message_id']}-photo.jpg]"
    )
    n = len(ai["turns"])
    b.on_message(photo(chat=LUIS, user=LUIS))  # a stranger's photo is ignored
    assert not b.db.unsent(LUIS) and len(ai["turns"]) == n
    turn(b, message("@watch_bot y sin foto"))
    assert ai["turns"][-1]["attachments"] == []


def test_a_file_of_any_kind_reaches_the_ai_with_its_own_name(world):
    b, ft, ai, _ = world
    pdf = document("Factura Ñandú 2026.v2.PDF", "application/pdf")
    turn(b, pdf)
    name = f"inbox/{pdf['message_id']}-Factura-Nandu-2026-v2.pdf"
    assert ai["turns"][-1]["new"][-1]["content"] == f"Ana: [file: {name}]"
    assert [(a["name"], a["mime"]) for a in ai["turns"][-1]["attachments"]] == [
        (name, "application/pdf")
    ]
    docx = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    sent = [
        document("informe final.docx", docx, file_id="w"),
        document("precios.csv", "text/csv", file_id="c"),
        document("copia.zip", "application/zip", file_id="z"),
        document("", None, file_id="x"),  # no name, no mime type
    ]
    for m in sent:
        b.on_message(m)
    b.turn(GROUP)
    got = [(a["name"].split("-", 1)[1], a["mime"]) for a in ai["turns"][-1]["attachments"]]
    assert got == [
        ("informe-final.docx", docx),
        ("precios.csv", "text/csv"),
        ("copia.zip", "application/zip"),
        ("file.bin", "application/octet-stream"),
    ]
    assert ai["turns"][-1]["new"][0]["content"].startswith("Ana: [file: inbox/")
    n = len(ai["turns"])
    b.on_message(document("enorme.pdf", "application/pdf", size=21 * 1024 * 1024))
    assert "demasiado grande" in ft.texts()[-1]
    assert not b.db.unsent(GROUP) and len(ai["turns"]) == n


def test_videos_gifs_video_notes_stickers_and_unheard_voice_notes_go_as_files(world):
    b, _ft, ai, _ = world

    def media(kind, **fields):
        msg = message("", chat=ANA)
        del msg["text"]
        msg[kind] = {"file_id": f"{kind}-{len(fields)}", "file_size": 1000, **fields}
        return msg

    gif = media("animation", mime_type="video/mp4", file_name="risa.gif.mp4")
    gif["document"] = dict(gif["animation"])  # Telegram sends a GIF with a document too
    sent = [
        gif,
        media("video", mime_type="video/quicktime", file_name="IMG_1.MOV"),
        media("video_note"),
        media("sticker"),
        media("sticker", is_animated=True),
        media("sticker", is_video=True, is_animated=False),
        media("voice", mime_type="audio/ogg"),  # no transcription provider: a file like the rest
    ]
    for m in sent:
        b.on_message(m)
    b.turn(ANA)
    t = ai["turns"][-1]
    got = [(a["name"].split("-", 1)[1], a["mime"]) for a in t["attachments"]]
    assert got == [
        ("animation.mp4", "video/mp4"),
        ("video.mov", "video/quicktime"),
        ("video-note.mp4", "video/mp4"),
        ("sticker.webp", "image/webp"),
        ("sticker.tgs", "application/x-tgsticker"),
        ("sticker.webm", "video/webm"),
        ("voice.ogg", "audio/ogg"),
    ]
    labels = [x["content"].split(":", 2)[1].strip(" [") for x in t["new"]]
    assert labels == [
        "animation",
        "video",
        "video note",
        "sticker",
        "sticker",
        "sticker",
        "voice note",
    ]


def test_a_file_that_cannot_be_fetched_says_so_and_its_line_goes_on(world):
    b, ft, ai, _ = world
    ft.files["big"] = {"error": "Bad Request: file is too big"}
    ft.files["gone"] = {"error": "Bad Request: wrong file_id"}
    big = document("a.pdf", "application/pdf", size=None, file_id="big")
    gone = document("b.pdf", "application/pdf", file_id="gone")
    b.on_message(big)
    b.on_message(gone)
    b.turn(GROUP)
    assert replies(ft)[-3:-1] == [
        (
            "Ese archivo es demasiado grande para mí (hasta 20 MB). ¿Me mandas una foto o una captura?",
            big["message_id"],
        ),
        ("No he podido descargar ese archivo. ¿Me lo vuelves a mandar?", gone["message_id"]),
    ]
    t = ai["turns"][-1]
    assert len(t["new"]) == 2 and t["attachments"] == [] and not b.db.unsent(GROUP)
    assert TOKEN not in json.dumps(ft.calls)


def test_files_past_the_turn_budget_and_those_sent_during_a_turn_wait(world, monkeypatch):
    b, ft, ai, _ = world
    mb = 1024 * 1024
    kicked = []
    monkeypatch.setattr(
        b, "worker", lambda chat: type("W", (), {"kick": lambda s: kicked.append(chat)})()
    )
    for i in range(3):
        ft.files[f"d{i}"] = {"file_path": f"documents/{i}.jpg"}
        b.on_message(document(f"{i}.jpg", "image/jpeg", size=10 * mb, file_id=f"d{i}"))
    late = photo("@watch_bot y esta")
    handed = []

    def converse(data_, t):
        ai["turns"].append(t)
        if len(ai["turns"]) == 1:  # a photo while the turn runs: never handed over
            b.on_message(late)
            handed.extend(t["incoming"]())
        return Reply("Vale.", conversation="c1")

    monkeypatch.setattr(llm, "converse", converse)
    kicked.clear()
    b.turn(GROUP)
    assert [a["name"].split("-", 1)[1] for a in ai["turns"][-1]["attachments"]] == [
        "0.jpg",
        "1.jpg",
    ]
    assert kicked and handed == []  # the third file waits, and its turn is asked for
    b.turn(GROUP)
    t = ai["turns"][-1]
    assert [a["name"].split("-", 1)[1] for a in t["attachments"]] == ["2.jpg", "photo.jpg"]
    assert t["new"][-1]["content"].endswith("-photo.jpg] y esta") and not b.db.unsent(GROUP)


def test_a_caption_edit_of_a_photo_is_ignored(world):
    b, _ft, ai, _ = world
    m = photo("@watch_bot mira")
    b.on_message(m)
    b.on_edit({**m, "caption": "@watch_bot mira esto otro", "edit_date": 1})
    b.turn(GROUP)
    assert ai["turns"][-1]["new"][-1]["content"].endswith("-photo.jpg] mira")
    b.on_edit({**m, "caption": "@watch_bot otra cosa", "edit_date": 2})
    assert not b.db.unsent(GROUP)


def test_budget_takes_the_first_file_always_and_text_lines_never_wait():
    mb = 1024 * 1024
    rows = [
        {"_id": 1, "file": {"size": 24 * mb}},
        {"_id": 2, "text": "hola"},
        {"_id": 3, "file": {"size": 2 * mb}},
        {"_id": 4, "file": {"size": None}},
    ]
    take, wait = botmod._budget(rows)
    assert [r["_id"] for r in take] == [1, 2] and [r["_id"] for r in wait] == [3, 4]
    assert botmod._budget([{"_id": 9, "file": {"size": 40 * mb}}])[0][0]["_id"] == 9
    # a size Telegram did not give counts as the most a file can be: two never share a turn
    unknown = [{"_id": 5, "file": {"size": None}}, {"_id": 6, "file": {"file_id": "x"}}]
    assert [[r["_id"] for r in x] for x in botmod._budget(unknown)] == [[5], [6]]


def test_a_failed_turn_with_files_waiting_does_not_run_again_by_itself(world, monkeypatch):
    b, _ft, ai, _ = world
    kicked = []
    monkeypatch.setattr(
        b, "worker", lambda chat: type("W", (), {"kick": lambda s: kicked.append(chat)})()
    )
    for i in range(3):
        b.on_message(
            document(f"{i}.pdf", "application/pdf", size=10 * 1024 * 1024, file_id=f"d{i}")
        )
    kicked.clear()
    ai["answers"].append(RuntimeError("down"))
    b.turn(GROUP)
    assert not kicked and len(b.db.unsent(GROUP)) == 3  # the next message brings them all again
    b.turn(GROUP)
    assert kicked == [GROUP] and len(b.db.unsent(GROUP)) == 1
