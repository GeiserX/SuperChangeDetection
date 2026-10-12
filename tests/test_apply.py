from superchangedetection import actions, render
from superchangedetection.cd import notifies
from superchangedetection.i18n import Translator
from superchangedetection.model import Settings
from tests.conftest import TOKEN, U1, U2


def settings(**kw) -> Settings:
    return Settings.model_validate(
        {"locale": "es", "changedetection": {"first_check_wait_s": 1, **kw}}
    )


def test_create_sets_our_notification_and_reports_what_it_sees(cd, instance):
    _, acts = actions.extract(
        "```actions\n- op: create\n  url: https://c.example/\n  title: C\n  include_filters: ['.x']\n```"
    )
    t = Translator("es")
    rs = actions.apply(cd, acts, settings(tag="Telegram"), TOKEN, -5, t)
    assert rs[0].ok and rs[0].text == "new text"
    w = instance.watches[rs[0].uuid]
    assert notifies(w, TOKEN) == -5
    assert w["notification_format"] == "text"
    assert "ref {{watch_uuid}}" in w["notification_body"]
    assert "Ha cambiado" in w["notification_body"]
    assert w["time_between_check"] == {
        "weeks": 0,
        "days": 0,
        "hours": 1,
        "minutes": 0,
        "seconds": 0,
    }
    assert w["tags"] == ["tag-telegram"]
    out = render.results(rs, t)
    assert "Vigilo «C»" in out and "new text" in out and "dímelo" in out


def test_update_repoints_a_foreign_watch_to_this_chat_and_rechecks(cd, instance):
    _, acts = actions.extract(
        f"```actions\n- op: update\n  watch: {U2}\n  set: {{trigger_text: ['Stock']}}\n```"
    )
    rs = actions.apply(cd, acts, settings(), TOKEN, -5, Translator("en"))
    w = instance.watches[U2]
    assert w["trigger_text"] == ["Stock"] and notifies(w, TOKEN) == -5
    assert ("GET", f"/watch/{U2}", {"recheck": "1"}) in instance.calls
    assert rs[0].ok and rs[0].text == ""  # no snapshot yet
    assert "(nothing" in render.results(rs, Translator("en"))


def test_delete_and_errors_are_reported(cd, instance):
    _, acts = actions.extract(
        f"```actions\n- op: delete\n  watch: {U1}\n- op: recheck\n  watch: 99999999-9999-4999-8999-999999999999\n```"
    )
    rs = actions.apply(cd, acts, settings(), TOKEN, -5, Translator("es"))
    assert U1 not in instance.watches
    assert rs[0].ok and not rs[1].ok and "404" in rs[1].text
    out = render.results(rs, Translator("es"))
    assert "Borrado «A»" in out and "No he podido recheck" in out


def test_a_check_that_has_not_run_yet_is_reported_as_pending(cd, instance, monkeypatch):
    monkeypatch.setattr(
        instance, "now", 0
    )  # the fake marks the first check as already run, undo that
    _, acts = actions.extract(
        "```actions\n- op: create\n  url: https://c.example/\n  title: C\n```"
    )

    def slow_create(fields):
        uuid = "44444444-4444-4444-8444-444444444444"
        instance.watches[uuid] = {"uuid": uuid, "last_checked": 0, **fields}
        return uuid

    monkeypatch.setattr(cd, "create", slow_create)
    rs = actions.apply(cd, acts, settings(), TOKEN, -5, Translator("es"))
    assert rs[0].ok and rs[0].pending and rs[0].text == ""
    assert "aún no ha terminado" in render.results(rs, Translator("es"))
