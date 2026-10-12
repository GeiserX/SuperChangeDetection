import yaml

from superchangedetection import context, fetch
from superchangedetection.i18n import LOCALES, Translator
from superchangedetection.model import Settings
from tests.conftest import TOKEN, U1, U2


def test_facts_number_watches_and_mark_ours(cd):
    s = Settings.model_validate({"timezone": "Europe/Madrid"})
    out = context.facts(cd, TOKEN, s, Translator("es"), -5)
    assert "## 1. A" in out and "## 2. B" in out
    assert "notifies this chat" in out and "notifies elsewhere" in out
    assert f"uuid: {U1}" in out and "include_filters: ['.p']" in out


def test_rules_name_the_language_and_the_default_interval():
    s = Settings.model_validate(
        {"locale": "es", "changedetection": {"default_interval": {"hours": 0, "days": 1}}}
    )
    out = context.rules(s, Translator("es"))
    assert "Answer in Spanish (Spain)" in out and "Default 1 days" in out and "```actions" in out


def test_files_carry_snapshots_diffs_and_pages(cd, monkeypatch):
    monkeypatch.setattr(fetch, "page", lambda url, *a, **k: f"<html>{url}</html>")
    s = Settings()
    out = context.files(cd, TOKEN, s, ["mira https://z.example/p, y https://z.example/p."])
    assert out["pages/1.html"].startswith("<!-- https://z.example/p -->")
    assert "pages/2.html" not in out
    assert out[f"snapshots/{U1}.txt"] == "price 12"
    assert out[f"diffs/{U1}.txt"] == "-price 10\n+price 12"
    assert f"snapshots/{U2}.txt" not in out


def test_clean_drops_scripts_and_caps():
    html = "<html><script>x=1</script><style>a{}</style><!-- c --><div class='p' style='x'>  hi  </div></html>"
    out = fetch.clean(html)
    assert "<script" not in out and "<style" not in out and "<!-- c -->" not in out
    assert "style=" not in out and "class='p'" in out
    big = "<p>" + "a" * 5000 + "</p>"
    assert fetch.clean(big, max_kb=1).endswith("<!-- truncated -->")


def test_locales_have_the_same_keys_and_spanish_has_its_accents():
    en = yaml.safe_load((LOCALES / "en.yaml").read_text(encoding="utf-8"))
    es = yaml.safe_load((LOCALES / "es.yaml").read_text(encoding="utf-8"))
    assert set(en) == set(es)
    assert "dímelo" in es["result_footer"] and "comprobación" in es["result_create_error"]
    assert "{{watch_uuid}}" in es["notification_body"] and "{{diff_url}}" in es["notification_body"]
