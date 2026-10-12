import pytest

from superchangedetection import actions
from superchangedetection.actions import ActionError
from tests.conftest import U1, U2


def block(yaml_text: str) -> str:
    return f"Hecho.\n\n```actions\n{yaml_text}\n```"


def test_extract_returns_text_and_actions():
    text, acts = actions.extract(block("- op: create\n  url: https://x.example/\n  title: X"))
    assert text == "Hecho."
    assert acts[0].op == "create" and acts[0].fields["url"] == "https://x.example/"


def test_no_block_means_no_actions():
    assert actions.extract("Sin cambios.") == ("Sin cambios.", None)


@pytest.mark.parametrize(
    "yaml_text",
    [
        "- op: fly",
        "- op: create\n  title: no url",
        "- op: create\n  url: ftp://x",
        "- op: update\n  watch: 1",
        "- op: update\n  watch: 1\n  set: {notification_urls: [x]}",
        "- op: update\n  watch: 1\n  set: {fetch_backend: curl}",
        "- op: create\n  url: https://x\n  time_between_check: {years: 1}",
        "- op: create\n  url: https://x\n  time_between_check: {hours: 0}",
        '- op: create\n  url: https://x\n  include_filters: [""]',
        "- op: create\n  url: https://x\n  webdriver_delay: true",
        "- op: delete\n  watch: 1\n  title: x",
        "- op: delete",
        "not: a list",
    ],
)
def test_invalid_blocks_are_rejected(yaml_text):
    with pytest.raises(ActionError):
        actions.extract(block(yaml_text))


def test_interval_is_normalised_and_switches_off_the_default():
    _, acts = actions.extract(
        block("- op: create\n  url: https://x\n  time_between_check: {hours: 6}")
    )
    f = acts[0].fields
    assert f["time_between_check"] == {
        "weeks": 0,
        "days": 0,
        "hours": 6,
        "minutes": 0,
        "seconds": 0,
    }
    assert f["time_between_check_use_default"] is False


def test_resolve_by_number_or_uuid():
    _, acts = actions.extract(
        block(f"- op: recheck\n  watch: 2\n- op: delete\n  watch: {U1.upper()}")
    )
    actions.resolve(acts, [(1, U1), (2, U2)])
    assert [a.watch for a in acts] == [U2, U1]
    _, bad = actions.extract(block("- op: recheck\n  watch: 7"))
    with pytest.raises(ActionError):
        actions.resolve(bad, [(1, U1)])
