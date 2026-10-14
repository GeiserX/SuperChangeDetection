"""Buttons under an answer (options, a confirm, an offer), the bot's rules about whom it answers, and
reminders: offered with buttons, posted at their time, listed and cancelled with /recordatorios."""

# ruff: noqa: F811  world and inline are test_bot's fixtures, imported so pytest finds them

from __future__ import annotations

import datetime as dt
import threading
import time
from zoneinfo import ZoneInfo

from superchangedetection import bot as botmod
from superchangedetection import context, llm
from superchangedetection.i18n import Translator
from superchangedetection.llm import Reply
from superchangedetection.model import Settings
from tests.conftest import U1
from tests.test_bot import (  # noqa: F401
    ANA,
    BOT_ID,
    GROUP,
    LUIS,
    inline,
    message,
    turn,
    world,
)

TZ = ZoneInfo("Europe/Madrid")


def tap(b, ft, sent: dict, n: int = 0, row: int | None = None, user=ANA, name="Ana"):
    """Tap the n-th button (of row `row`, or of every row flattened) of a message the bot sent."""
    rows = sent["reply_markup"]["inline_keyboard"]
    flat = rows[row] if row is not None else [x for r in rows for x in r]
    mid = sent.get("_id") or 900
    b.on_callback(
        {
            "id": "q",
            "from": {"id": user, "first_name": name},
            "message": {
                "message_id": mid,
                "chat": {"id": GROUP, "type": "group"},
                "reply_markup": sent["reply_markup"],
            },
            "data": flat[n]["callback_data"],
        }
    )


def toast(ft) -> str | None:
    return ft.sent("answerCallbackQuery")[-1].get("text")


def markups(ft) -> list[dict]:
    return [p.get("reply_markup") for p in ft.sent("editMessageReplyMarkup")]


def labels(markup: dict | None) -> list[str]:
    return [x["text"] for r in (markup or {}).get("inline_keyboard", []) for x in r]


def at(days: int = 2, hour: int = 9) -> tuple[str, float]:
    d = (dt.datetime.now(TZ) + dt.timedelta(days=days)).replace(
        hour=hour, minute=0, second=0, microsecond=0
    )
    return f"{d:%Y-%m-%d %H:%M}", d.timestamp()


# ---- parsing


def test_the_blocks_come_out_of_the_text_and_bad_ones_are_dropped():
    text = "Hola.\n\n```options\n- Cada hora\n- Cada día\n- Cada hora\n```"
    assert llm.extract_buttons(text) == (
        "Hola.",
        {"kind": "options", "labels": ["Cada hora", "Cada día"]},
    )
    assert llm.extract_buttons("¿Sigo?\n```confirm\n```")[1] == {"kind": "confirm", "labels": []}
    assert llm.extract_buttons("Puedo.\n```offer\n```")[1] == {"kind": "offer", "labels": []}
    assert llm.extract_buttons("x\n```options\n```") == ("x", None)  # options need a label
    assert llm.buttons("menu", ["a"]) is None
    assert llm.buttons("options", ["x" * 41, "ok", 3, None])["labels"] == ["ok", "3"]
    clean, rs = llm.extract_reminders(
        "Vale.\n```reminders\n- 2026-11-20 09:00 | Mira la web | 3\n- mañana | mal\n"
        "- 2026-11-20T10:00 | Otra\n```"
    )
    assert clean == "Vale."
    assert rs == [
        {"when": "2026-11-20 09:00", "text": "Mira la web", "watch": "3"},
        {"when": "2026-11-20 10:00", "text": "Otra", "watch": None},
    ]


def test_a_plugin_may_return_buttons_and_reminders_beside_the_text(tmp_path, monkeypatch):
    (tmp_path / "plugins").mkdir()
    (tmp_path / "plugins" / "p.py").write_text(
        "def converse(turn, settings):\n"
        "    return {'text': 'Hola', 'buttons': {'kind': 'offer', 'labels': ['Vale']},\n"
        "            'reminders': [{'when': '2026-11-20 09:00', 'text': 'Mira'}, 'x']}\n",
        encoding="utf-8",
    )
    (tmp_path / "settings.yaml").write_text(
        "llm: {provider: plugin, plugin: plugins/p.py}\n", encoding="utf-8"
    )
    from superchangedetection.model import Data

    r = llm.converse(Data(tmp_path), {"rules": "", "context": "", "new": []})
    assert r.buttons == {"kind": "offer", "labels": ["Vale"]}
    assert r.reminders == [{"when": "2026-11-20 09:00", "text": "Mira", "watch": None}]


# ---- the rules


def test_the_rules_say_whom_it_answers_and_offer_the_exact_labels():
    rules = context.rules(Settings(locale="es", timezone="Europe/Madrid"), Translator("es"))
    assert "## Who you answer" in rules and "is not a request" in rules
    assert "Act only on what is addressed to you." in rules
    assert "start with `chat:` and are context only" in rules
    assert '"Cada hora" (an interval of 1 hour), "Cada día" (1 day), "Solo una vez"' in rules
    assert '"Nada" (nothing)' in rules
    assert "```reminders" in rules and "Europe/Madrid" in rules and "/recordatorios" in rules
    assert "Never offer buttons for a delete" in rules


# ---- buttons under an answer


def test_options_go_under_the_answer_and_a_tap_is_the_askers_next_message(world):
    b, ft, ai, _ = world
    ai["answers"] = ["Vigilaría el precio.\n\n```options\n- Cada hora\n- Cada día\n```"]
    turn(b, message("@watch_bot https://tienda.example/x"))
    sent = ft.sent()[-1]
    assert sent["text"].startswith("Vigilaría el precio.") and "```" not in sent["text"]
    assert sent["text"].endswith("<i>O cuéntame otra cosa.</i>")
    assert labels(sent["reply_markup"]) == ["Cada hora", "Cada día"]
    mid = b.db.get_kv("buttons")[0]["msg"]
    tap(b, ft, sent, 1, user=LUIS, name="Luis")  # not the asker
    assert toast(ft) == "Solo Ana puede responder a esto." and not b.db.unsent(GROUP)
    tap(b, ft, sent, 1)
    assert labels(markups(ft)[-1]) == ["✓ Cada día"]
    (row,) = b.db.unsent(GROUP)
    assert row["text"] == '«Cada día» (tapped under your message: "Vigilaría el precio.")'
    assert row["msg"] == mid and row["user"] == ANA and row["asked"] is True
    tap(b, ft, sent, 0)  # chosen once
    assert toast(ft) == "Esto ya no está esperando respuesta." and len(b.db.unsent(GROUP)) == 1
    b.turn(GROUP)  # the answer to the tap replies to the question it came from
    assert ft.sent()[-1]["reply_parameters"]["message_id"] == mid


def test_a_confirm_shows_yes_and_no_and_says_they_can_reply_otherwise(world):
    b, ft, ai, _ = world
    ai["answers"] = ["¿Quito el contador de visitas?\n```confirm\n```"]
    turn(b, message("@watch_bot me avisa sin motivo"))
    sent = ft.sent()[-1]
    assert labels(sent["reply_markup"]) == ["Sí", "No"]
    assert sent["text"].endswith("<i>Si quieres otra cosa, responde a este mensaje.</i>")


def test_an_offer_expires_and_then_says_nobody_answered(world, monkeypatch):
    b, ft, ai, _ = world
    ai["answers"] = ["Hecho.\n```offer\n- Vigila también /precios\n- Nada\n```"]
    turn(b, message("@watch_bot vigila https://a.example/x"))
    sent = ft.sent()[-1]
    assert sent["text"].endswith("<i>Si no respondes, no hago nada.</i>")
    real = botmod._now()
    b.expire_buttons()  # not yet
    assert b.db.get_kv("buttons") and not markups(ft)
    monkeypatch.setattr(botmod, "_now", lambda: real + botmod.OFFER_BUTTONS_TTL_S + 1)
    b.expire_buttons()
    assert labels(markups(ft)[-1]) == ["Sin respuesta: nada"] and not b.db.get_kv("buttons")
    tap(b, ft, sent, 0)
    assert toast(ft) == "Esto ya no está esperando respuesta." and not b.db.unsent(GROUP)


def test_options_lose_their_buttons_after_half_a_day_and_offers_after_half_an_hour(
    world, monkeypatch
):
    b, ft, ai, _ = world
    ai["answers"] = ["¿Cuál?\n```options\n- Precio\n- Stock\n```"]
    turn(b, message("@watch_bot vigila https://a.example/x"))
    real = botmod._now()
    monkeypatch.setattr(botmod, "_now", lambda: real + botmod.OFFER_BUTTONS_TTL_S + 1)
    b.expire_buttons()
    assert not markups(ft) and b.db.get_kv("buttons")  # options outlive an offer
    monkeypatch.setattr(botmod, "_now", lambda: real + botmod.BUTTONS_TTL_S + 1)
    b.expire_buttons()
    assert markups(ft)[-1] is None and not b.db.get_kv("buttons")


def test_a_tap_from_someone_not_allowed_does_nothing(world):
    b, ft, ai, _ = world
    ai["answers"] = [Reply("¿Cuál?", "c1", buttons={"kind": "options", "labels": ["A", "B"]})]
    turn(b, message("hola", chat=ANA, user=ANA))
    sent = ft.sent()[-1]
    stranger = {
        "id": "q",
        "from": {"id": 999, "first_name": "X"},
        "message": {"message_id": 1, "chat": {"id": ANA, "type": "private"}},
        "data": sent["reply_markup"]["inline_keyboard"][0][0]["callback_data"],
    }
    b.on_callback(stranger)
    assert not b.db.unsent(ANA) and not markups(ft)
    rid = b.db.add_reminder(ANA, ANA, "Ana", time.time() + 3600, "Mira")
    b.on_callback({**stranger, "data": f"can:{rid}:1"})
    assert b.db.reminder(rid)["state"] == "pending" and not markups(ft)


# ---- reminders


def test_a_reminder_is_offered_set_with_a_tap_and_posted_at_its_time(world, monkeypatch):
    b, ft, ai, _ = world
    when, ts = at(2)
    past, _ = at(-1)
    ai["answers"] = [f"Vale.\n```reminders\n- {when} | Mira la página | 1\n- {past} | Ya pasó\n```"]
    turn(b, message("@watch_bot recuérdame mirar esta página el viernes"))
    offer = ft.sent()[-1]
    assert offer["text"].startswith("Ana, ¿quieres que te lo recuerde?\n")
    assert (
        "Mira la página (y vuelvo a mirar «A»)" in offer["text"] and "Ya pasó" not in offer["text"]
    )
    assert offer["text"].endswith("Si no respondes, no hago nada.")
    assert labels(offer["reply_markup"]) == ["Recordatorio aquí", "Nada"]
    tap(b, ft, offer, 1, user=LUIS, name="Luis")
    assert toast(ft) == "Solo Ana puede responder a esto." and not b.db.reminders(GROUP)
    tap(b, ft, offer, 0)
    assert toast(ft).startswith("Te lo recuerdo aquí el ")
    assert labels(markups(ft)[-1]) == ["✓ Recordatorio aquí"]
    (r,) = b.db.reminders(GROUP)
    assert (r["user"], r["who"], r["at"], r["watch"]) == (ANA, "Ana", ts, U1)
    assert "Ana set a reminder here for" in b.db.get_kv(f"app:{GROUP}")
    n = len(ft.sent())
    b.tick_reminders()
    assert len(ft.sent()) == n  # not yet
    rechecked = []
    monkeypatch.setattr(b, "later", lambda fn, *a: rechecked.append((fn.__name__, a)))
    monkeypatch.setattr(botmod, "_now", lambda: int(ts) + 5)
    b.tick_reminders()
    posted = ft.sent()[-1]
    assert posted["text"] == f'⏰ <a href="tg://user?id={ANA}">Ana</a>: Mira la página'
    assert posted.get("parse_mode") == "HTML"
    assert [(f, a[0]["id"]) for f, a in rechecked] == [("recheck_for", r["id"])]
    assert b.db.reminder(r["id"])["state"] == "sent"
    b.tick_reminders()
    assert len(ft.sent()) == n + 1  # once


def test_a_reminders_watch_is_checked_again_and_the_chat_sees_it(world, inline):
    b, ft, _ai, _ = world
    rid = b.db.add_reminder(GROUP, ANA, "Ana", time.time() - 1, "Mira", U1, "A")
    b.tick_reminders()
    assert "⏰" in ft.texts()[-2] and "Comprobado «A» ahora mismo" in ft.texts()[-1]
    assert b.db.reminder(rid)["state"] == "sent"


def test_a_late_reminder_says_so(world, monkeypatch):
    b, ft, _ai, _ = world
    b.db.add_reminder(GROUP, ANA, "Ana", botmod._now() - 3600, "Mira")
    b.tick_reminders()
    assert "(con retraso: era para el " in ft.texts()[-1]


def test_an_untapped_reminder_offer_expires_and_sets_nothing(world, monkeypatch):
    b, ft, ai, _ = world
    when, _ = at(3)
    ai["answers"] = [f"Vale.\n```reminders\n- {when} | Mira\n```"]
    turn(b, message("@watch_bot recuérdamelo"))
    offer = ft.sent()[-1]
    real = botmod._now()
    monkeypatch.setattr(botmod, "_now", lambda: real + botmod.OFFER_BUTTONS_TTL_S + 1)
    b.expire_buttons()
    assert labels(markups(ft)[-1]) == ["✓ Sin respuesta: nada"] and not b.db.get_kv("remind")
    tap(b, ft, offer, 0)
    assert toast(ft) == "Esto ya no está esperando respuesta." and not b.db.reminders(GROUP)


def test_nothing_is_a_choice_too_and_the_ai_hears_it(world):
    b, ft, ai, _ = world
    w1, _ = at(2)
    w2, _ = at(3)
    ai["answers"] = [f"Vale.\n```reminders\n- {w1} | Uno\n- {w2} | Dos\n```"]
    turn(b, message("@watch_bot dos cosas"))
    offer = ft.sent()[-1]
    assert labels(offer["reply_markup"]) == [
        "1 · Recordatorio aquí",
        "1 · Nada",
        "2 · Recordatorio aquí",
        "2 · Nada",
    ]
    tap(b, ft, offer, 1, row=0)
    assert labels(markups(ft)[-1])[:1] == ["✓ 1 · Nada"] and b.db.get_kv("remind")
    assert "Ana chose no reminder for" in b.db.get_kv(f"app:{GROUP}")
    tap(b, ft, offer, 0, row=1)
    assert not b.db.get_kv("remind") and [r["text"] for r in b.db.reminders(GROUP)] == ["Dos"]


def test_recordatorios_lists_them_and_a_tap_cancels_one(world, monkeypatch):
    b, ft, _ai, _ = world
    b.on_message(message("/recordatorios"))
    assert ft.texts()[-1] == "No hay recordatorios pendientes en este chat."
    _, ts = at(2)
    rid = b.db.add_reminder(GROUP, ANA, "Ana", ts, "Mira", U1, "A")
    b.db.add_reminder(GROUP, ANA, "Ana", ts + 60, "Otra")
    b.on_message(message("/recordatorios"))
    listed = ft.sent()[-1]
    assert listed["text"].startswith("⏰ Recordatorios pendientes:\n1. ")
    assert " · Ana: Mira (y vuelvo a mirar «A»)" in listed["text"]
    assert labels(listed["reply_markup"]) == ["Cancelar 1", "Cancelar 2"]
    tap(b, ft, listed, 0, user=LUIS, name="Luis")  # anyone in the chat may cancel
    assert toast(ft) == "Cancelado."
    assert labels(markups(ft)[-1]) == ["✓ Cancelado 1", "Cancelar 2"]
    assert b.db.reminder(rid)["state"] == "cancelled"
    tap(b, ft, listed, 0)
    assert toast(ft) == "Esto ya no está esperando respuesta."
    monkeypatch.setattr(botmod, "_now", lambda: int(ts) + 120)
    b.tick_reminders()
    assert ft.texts()[-1].endswith(": Otra")
    assert not any("Mira" in t for t in ft.texts() if t.startswith("⏰ <a"))


def test_the_tick_runs_on_the_bots_own_loop(world, monkeypatch):
    b, ft, _ai, _ = world
    b.db.add_reminder(GROUP, ANA, "Ana", botmod._now() - 1, "Mira")
    monkeypatch.setattr(botmod, "FOLLOWUP_EVERY_S", 0.01)
    stop = threading.Event()
    threading.Thread(target=b._followups, args=(stop,), daemon=True).start()
    for _ in range(200):
        if any(t.startswith("⏰") for t in ft.texts()):
            break
        time.sleep(0.01)
    stop.set()
    assert any(t.endswith(": Mira") for t in ft.texts())


def test_help_intro_and_menu_name_the_reminders_command(world):
    b, ft, _ai, _ = world
    b.on_message(message("/ayuda"))
    assert "/recordatorios: los recordatorios pendientes" in ft.texts()[-1]
    b.on_message(message("/presentacion"))
    intro = ft.texts()[-1]
    assert "un comentario entre vosotros sobre mí no es una orden" in intro
    assert "si no respondes, no hago nada" in intro and "/recordatorios" in intro
    menus = [p for m, p in ft.calls if m == "setMyCommands"]
    assert all("recordatorios" in [c["command"] for c in p["commands"]] for p in menus)


def test_a_from_bot_reply_is_still_answered_after_a_tap(world):
    """A tapped line rides as a message from the person: a later typed reply still works."""
    b, ft, ai, _ = world
    ai["answers"] = ["¿Cuál?\n```options\n- A\n- B\n```", "Vale."]
    turn(b, message("@watch_bot vigila"))
    mid = b.db.get_kv("buttons")[0]["msg"]
    turn(b, message("la otra", reply_to={"message_id": mid, "from": {"id": BOT_ID}}))
    assert ft.texts()[-1] == "Vale."
