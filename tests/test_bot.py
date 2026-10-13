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

    def handler(self, request: httpx.Request) -> httpx.Response:
        method = request.url.path.rsplit("/", 1)[-1]
        assert f"/bot{TOKEN}/" in request.url.path
        params = json.loads(request.content) if request.content else {}
        self.calls.append((method, params))
        if method == "getMe":
            return self._ok(
                {"id": BOT_ID, "username": "watch_bot", "can_read_all_group_messages": False}
            )
        if method == "getUpdates":
            out, self.updates = self.updates, []
            return self._ok(out)
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
    assert "¿Borro «A»?" in offer["text"] and "Ana" in offer["text"]
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
    cmds = ft.sent("setMyCommands")[0]
    assert cmds["language_code"] == "es" and cmds["commands"][0]["command"] == "lista"
    assert ft.sent("setMyDescription")[0]["description"].startswith("Dime")
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
    real = "1234567890:AAEabcdefghijklmnopqrstuvwxyz012345"
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
