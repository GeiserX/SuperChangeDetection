"""What the AI gets on every turn: its rules, the instance's watches as of now (context.md), and
files it can read: the HTML of every page named in the new messages, and each watch's latest
text and last change. The rules are English; the answers come in the chat's language."""

from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo

from . import fetch
from .cd import CD, notifies
from .i18n import Translator
from .model import Settings

RULES = """# You are the watch assistant of this chat

People write to you in a chat to watch web pages for changes. You create and adjust watches on a
changedetection.io instance. A watch fetches a page on a schedule, keeps the text that passes its
filters, and the chat gets a message when that text changes. Nothing else involves you: the
notification goes out on its own.

## How to answer
- Answer in {language}, short, plain, no jargon. One message, no preamble, no notes about files or
  tools. Your last message goes to the chat exactly as you write it.
- When someone wants something watched, do it in this same answer: read the page, choose the
  filters, and put the action in the block below. Do not ask what you can decide yourself (a title,
  the interval, the selector). Ask only when the page or the request is genuinely ambiguous.
- Say in one line what you will watch and how often. The app then shows what the watch sees now.
- When someone says a watch fires too often, or missed a change, or watches the wrong thing: read
  snapshots/<uuid>.txt and diffs/<uuid>.txt, find the noise or the gap, and fix it with an update.
  Typical fixes: a tighter include_filters, ignore_text for counters, dates and "views" lines,
  trigger_text so only lines with that text count, extract_lines_containing to keep only the lines
  that matter, a longer interval.
- Never invent a URL. Never touch a watch nobody asked about.

## Choosing filters
- pages/<n>.html is the page as fetched now, scripts and styles removed. Find the element that
  holds what the person cares about and write a CSS selector for it (or an XPath starting with
  `//` or `xpath:`). Prefer stable ids and class names; avoid positional paths unless nothing else
  exists. Put it in include_filters. Several selectors are joined.
- If the HTML has no real content (a skeleton, "enable JavaScript", an empty app root), set
  fetch_backend: html_webdriver so a real browser renders it, and watch the whole text or use
  trigger_text on what matters.
- A price, a stock state, a version number, a date: extract just that. A news list: the list's
  container, with ignore_text for timestamps. A whole page: no include_filters at all.
- time_between_check: pick from the request. Default {interval}. Prices and stock: hours.
  Release notes and official pages: a day. Nothing under 5 minutes.
- title: short, in {language}, what the person would call it.

## Actions
End your answer with ONE fenced block when something must change. Omit it otherwise.

```actions
- op: create
  url: https://example.com/product
  title: Precio del teclado
  include_filters: [".product-price"]
  time_between_check: {{hours: 6}}
- op: update
  watch: 3            # the number from the list in context.md, or the uuid
  set: {{ignore_text: ["Visitas:"], trigger_text: ["En stock"]}}
- op: delete
  watch: 2
- op: recheck
  watch: 1
```

Fields allowed in create and in set: url, title, include_filters, subtractive_selectors,
ignore_text, trigger_text, text_should_not_be_present, extract_lines_containing, extract_text
(regex), trim_text_whitespace, sort_text_alphabetically, remove_duplicate_lines,
check_unique_lines, filter_text_added, filter_text_removed, filter_text_replaced,
time_between_check (weeks/days/hours/minutes/seconds), fetch_backend (system, html_requests,
html_webdriver), webdriver_delay, paused, notification_muted, processor (text_json_diff,
restock_diff for stock and price pages), in_stock_only, follow_price_changes,
price_change_threshold_percent, headers, method, body, ignore_status_codes, tags (names).
The app sets where the notification goes and what it says; you never do. A delete is confirmed by
the person before it happens. Set lists replace the old list, so repeat what must stay.
"""


def rules(settings: Settings, t: Translator) -> str:
    out = RULES.format(language=t("language_name"), interval=_interval(settings))
    if settings.llm.instructions:
        out += "\n## This instance\n" + settings.llm.instructions.strip() + "\n"
    return out


def _interval(settings: Settings) -> str:
    iv = settings.changedetection.default_interval.model_dump()
    parts = [f"{v} {k}" for k, v in iv.items() if v]
    return " ".join(parts) or "1 hours"


def listing(cd: CD, token: str) -> list[tuple[int, str, dict]]:
    """Every watch on the instance, numbered in a stable order: oldest first."""
    ws = cd.watches()
    order = sorted(ws.items(), key=lambda kv: (kv[1].get("date_created") or 0, kv[0]))
    return [(n, uuid, w) for n, (uuid, w) in enumerate(order, 1)]


def facts(cd: CD, token: str, settings: Settings, t: Translator, chat_id: int | None) -> str:
    tz = ZoneInfo(settings.timezone)
    now = dt.datetime.now(tz)
    lines = [f"# Watches as of {now:%Y-%m-%d %H:%M} ({settings.timezone})", ""]
    rows = listing(cd, token)
    if not rows:
        lines.append("No watches yet.")
    for n, uuid, w in rows:
        to = notifies(w, token)
        where = (
            "notifies this chat"
            if to == chat_id
            else f"notifies chat {to}"
            if to is not None
            else "notifies elsewhere (not created by this bot)"
        )
        lines.append(f"## {n}. {w.get('title') or '(no title)'}")
        lines.append(f"- uuid: {uuid}")
        lines.append(f"- url: {w.get('url')}")
        lines.append(f"- {where}")
        for k in (
            "include_filters",
            "subtractive_selectors",
            "ignore_text",
            "trigger_text",
            "text_should_not_be_present",
            "extract_lines_containing",
            "extract_text",
        ):
            if w.get(k):
                lines.append(f"- {k}: {w[k]}")
        if not w.get("time_between_check_use_default", True):
            iv = {k: v for k, v in (w.get("time_between_check") or {}).items() if v}
            lines.append(f"- time_between_check: {iv}")
        else:
            lines.append("- time_between_check: the instance default")
        lines.append(f"- fetch_backend: {w.get('fetch_backend') or 'system'}")
        if w.get("processor") and w["processor"] != "text_json_diff":
            lines.append(f"- processor: {w['processor']}")
        if w.get("paused"):
            lines.append("- paused: true")
        if w.get("notification_muted"):
            lines.append("- notifications muted")
        lines.append(f"- last_checked: {_when(w.get('last_checked'), tz)}")
        lines.append(f"- last_changed: {_when(w.get('last_changed'), tz)}")
        if w.get("last_error"):
            lines.append(f"- last_error: {str(w['last_error'])[:300]}")
        lines.append("")
    return "\n".join(lines)


def _when(ts, tz) -> str:
    try:
        ts = int(ts or 0)
    except (TypeError, ValueError):
        return "never"
    if ts <= 0:
        return "never"
    return dt.datetime.fromtimestamp(ts, tz).strftime("%Y-%m-%d %H:%M")


def files(cd: CD, token: str, settings: Settings, texts: list[str]) -> dict[str, str]:
    """pages/<n>.html for every URL in the new messages, snapshots/ and diffs/ for every watch."""
    out: dict[str, str] = {}
    urls: list[str] = []
    for text in texts:
        for u in fetch.urls_in(text):
            if u not in urls:
                urls.append(u)
    if settings.fetch.enabled:
        for n, u in enumerate(urls[:5], 1):
            html = fetch.page(
                u, settings.fetch.timeout_s, settings.fetch.user_agent, settings.fetch.max_kb
            )
            out[f"pages/{n}.html"] = f"<!-- {u} -->\n{html}"
    for _, uuid, w in listing(cd, token):
        snap = cd.snapshot(uuid).strip()
        if snap:
            out[f"snapshots/{uuid}.txt"] = snap[: settings.snapshot_chars]
        diff = cd.diff(uuid).strip()
        if diff:
            out[f"diffs/{uuid}.txt"] = diff[: settings.diff_chars]
    return out
