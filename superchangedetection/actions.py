"""What the AI may do to a watch, and how the app does it.

The AI ends an answer with one fenced block of YAML, a list of actions:

    ```actions
    - op: create
      url: https://example.com/product
      title: Precio del teclado
      include_filters: [".price"]
      trigger_text: []
      time_between_check: {hours: 6}
    - op: update
      watch: 3                      # the number from the list, or the uuid
      set: {ignore_text: ["Visitas:"], trigger_text: ["Disponible"]}
    - op: delete
      watch: 2
    - op: recheck
      watch: 1
    ```

Only the fields in ALLOWED reach changedetection; the notification fields are the app's (every watch
the bot creates notifies the chat that asked, with the app's own template), and nothing else on the
instance can be touched. The app validates the block, applies it, and reports what the watch sees
now, so a wrong selector shows at once.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field

import yaml

from .cd import CD, CDError, notifies, tgram_url
from .i18n import Translator
from .model import Settings

BLOCK = re.compile(r"```actions?\s*\n(.*?)```", re.DOTALL)
UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
OPS = ("create", "update", "delete", "recheck")

# field -> type the value must have (a tuple means any of them)
ALLOWED: dict[str, type | tuple] = {
    "url": str,
    "title": str,
    "include_filters": list,
    "subtractive_selectors": list,
    "ignore_text": list,
    "trigger_text": list,
    "text_should_not_be_present": list,
    "extract_lines_containing": list,
    "extract_text": list,
    "trim_text_whitespace": bool,
    "sort_text_alphabetically": bool,
    "remove_duplicate_lines": bool,
    "check_unique_lines": bool,
    "filter_text_added": bool,
    "filter_text_removed": bool,
    "filter_text_replaced": bool,
    "time_between_check": dict,
    "fetch_backend": str,
    "webdriver_delay": int,
    "paused": bool,
    "notification_muted": bool,
    "processor": str,
    "in_stock_only": bool,
    "follow_price_changes": bool,
    "price_change_threshold_percent": (int, float),
    "headers": dict,
    "method": str,
    "body": str,
    "ignore_status_codes": bool,
    "tags": list,  # tag NAMES; the app maps them to the instance's tag ids
}
INTERVAL_KEYS = ("weeks", "days", "hours", "minutes", "seconds")
BACKENDS = ("system", "html_requests", "html_webdriver")
PROCESSORS = ("text_json_diff", "restock_diff")


class ActionError(ValueError):
    pass


@dataclass
class Action:
    op: str
    watch: str | None = None  # uuid after resolve()
    fields: dict = field(default_factory=dict)


def extract(text: str) -> tuple[str, list[Action] | None]:
    """The answer without the block, and the actions (None when there is no block).
    A malformed block raises ActionError with the reason."""
    m = BLOCK.search(text or "")
    if not m:
        return (text or "").strip(), None
    clean = (text[: m.start()] + text[m.end() :]).strip()
    try:
        raw = yaml.safe_load(m.group(1)) or []
    except yaml.YAMLError as e:
        raise ActionError(f"not valid YAML: {e}") from e
    if isinstance(raw, dict):
        raw = [raw]
    if not isinstance(raw, list):
        raise ActionError("the block must be a list of actions")
    return clean, [_parse(i, a) for i, a in enumerate(raw, 1)]


def _parse(i: int, a) -> Action:
    if not isinstance(a, dict) or a.get("op") not in OPS:
        raise ActionError(f"action {i}: op must be one of {', '.join(OPS)}")
    op = a["op"]
    fields = {k: v for k, v in a.items() if k not in ("op", "watch", "set")}
    if op == "update":
        if not isinstance(a.get("set"), dict) or not a["set"]:
            raise ActionError(f"action {i}: update needs a non-empty set")
        fields = dict(a["set"])
    if op in ("update", "delete", "recheck") and a.get("watch") in (None, ""):
        raise ActionError(f"action {i}: {op} needs watch")
    if op in ("delete", "recheck") and fields:
        raise ActionError(f"action {i}: {op} takes no fields")
    if op == "create" and not isinstance(fields.get("url"), str):
        raise ActionError(f"action {i}: create needs url")
    for k, v in fields.items():
        if k not in ALLOWED:
            raise ActionError(f"action {i}: field {k!r} is not allowed")
        want = ALLOWED[k]
        if isinstance(want, type) and want is int and isinstance(v, bool):
            raise ActionError(f"action {i}: {k} must be an integer")
        if not isinstance(v, want):
            raise ActionError(f"action {i}: {k} has the wrong type")
    if "url" in fields and not re.match(r"^https?://", fields["url"]):
        raise ActionError(f"action {i}: url must start with http:// or https://")
    if "fetch_backend" in fields and fields["fetch_backend"] not in BACKENDS:
        raise ActionError(f"action {i}: fetch_backend must be one of {', '.join(BACKENDS)}")
    if "processor" in fields and fields["processor"] not in PROCESSORS:
        raise ActionError(f"action {i}: processor must be one of {', '.join(PROCESSORS)}")
    if "time_between_check" in fields:
        tbc = fields["time_between_check"]
        bad = [k for k in tbc if k not in INTERVAL_KEYS]
        if bad or not all(isinstance(v, int) and v >= 0 for v in tbc.values()):
            raise ActionError(
                f"action {i}: time_between_check takes weeks/days/hours/minutes/seconds"
            )
        if not any(tbc.values()):
            raise ActionError(f"action {i}: time_between_check must not be zero")
        fields["time_between_check"] = {k: tbc.get(k, 0) for k in INTERVAL_KEYS}
        fields["time_between_check_use_default"] = False
    for k in (
        "include_filters",
        "subtractive_selectors",
        "ignore_text",
        "trigger_text",
        "text_should_not_be_present",
        "extract_lines_containing",
        "extract_text",
        "tags",
    ):
        if k in fields and not all(isinstance(x, str) and x.strip() for x in fields[k]):
            raise ActionError(f"action {i}: {k} must be a list of non-empty strings")
    watch = a.get("watch")
    return Action(op, str(watch) if watch is not None else None, fields)


def resolve(actions: list[Action], listing: list[tuple[int, str]]) -> None:
    """Turn a watch number into its uuid in place. `listing` is [(number, uuid), ...] as shown."""
    by_num = {str(n): u for n, u in listing}
    uuids = {u for _, u in listing}
    for a in actions:
        if a.watch is None:
            continue
        if a.watch in by_num:
            a.watch = by_num[a.watch]
        elif a.watch.lower() in uuids:
            a.watch = a.watch.lower()
        else:
            raise ActionError(f"unknown watch {a.watch!r}")


@dataclass
class Result:
    op: str
    uuid: str | None
    title: str
    url: str
    ok: bool
    text: str = ""  # what the watch sees now, or the error
    failed: bool = False  # the instance refused or could not be reached
    pending: bool = False  # the check had not finished when we stopped waiting


def apply(
    cd: CD,
    actions: list[Action],
    settings: Settings,
    token: str,
    chat_id: int,
    t: Translator,
) -> list[Result]:
    out = []
    for a in actions:
        try:
            out.append(_apply_one(cd, a, settings, token, chat_id, t))
        except CDError as e:
            out.append(Result(a.op, a.watch, "", a.fields.get("url", ""), False, str(e), True))
    return out


def _apply_one(
    cd, a: Action, settings: Settings, token: str, chat_id: int, t: Translator
) -> Result:
    cfg = settings.changedetection
    fields = dict(a.fields)
    if "tags" in fields:
        names = list(fields["tags"])
        if cfg.tag and cfg.tag not in names:
            names.append(cfg.tag)
        fields["tags"] = [cd.tag_uuid(n) for n in names]
    if a.op == "create":
        fields.setdefault("fetch_backend", cfg.fetch_backend)
        fields.setdefault("processor", "text_json_diff")
        if "time_between_check" not in fields:
            fields["time_between_check"] = cfg.default_interval.model_dump()
            fields["time_between_check_use_default"] = False
        if cfg.tag and "tags" not in fields:
            fields["tags"] = [cd.tag_uuid(cfg.tag)]
        fields.update(notification(settings, token, chat_id, t))
        since = int(time.time())
        uuid = cd.create(fields)
        w = cd.wait_checked(uuid, since, cfg.first_check_wait_s)
        return _checked("create", w, since, cd, settings, fields["url"], fields.get("title", ""))
    if a.op == "update":
        w = cd.watch(a.watch)
        if notifies(w, token) is None:
            fields.update(notification(settings, token, chat_id, t))
        since = int(time.time())
        cd.update(a.watch, fields)
        cd.recheck(a.watch)
        w = cd.wait_checked(a.watch, since, cfg.first_check_wait_s)
        return _checked("update", w, since, cd, settings)
    if a.op == "delete":
        w = cd.watch(a.watch)
        cd.delete(a.watch)
        return Result("delete", a.watch, w.get("title") or "", w.get("url", ""), True)
    if a.op == "recheck":
        since = int(time.time())
        cd.recheck(a.watch)
        w = cd.wait_checked(a.watch, since, cfg.first_check_wait_s)
        return _checked("recheck", w, since, cd, settings)
    raise ActionError(a.op)


def _checked(op: str, w: dict, since: int, cd: CD, settings: Settings, url="", title="") -> Result:
    """The result once the check after `since` ran; pending when it had not by the deadline."""
    pending = int(w.get("last_checked") or 0) <= since
    return Result(
        op,
        w.get("uuid"),
        w.get("title") or title,
        w.get("url") or url,
        pending or not w.get("last_error"),
        "" if pending else _seen(cd, w, settings),
        pending=pending,
    )


def _seen(cd: CD, w: dict, settings: Settings) -> str:
    if w.get("last_error"):
        return str(w["last_error"])
    text = cd.snapshot(w["uuid"]) if w.get("uuid") else ""
    text = text.strip()
    limit = 600
    return text if len(text) <= limit else text[:limit].rstrip() + "…"


def notification(settings: Settings, token: str, chat_id: int, t: Translator) -> dict:
    """The notification fields of a watch the bot owns: our bot, the asking chat, our template."""
    return {
        "notification_urls": [tgram_url(token, chat_id)],
        "notification_title": t("notification_title"),
        "notification_body": t("notification_body"),
        "notification_format": settings.changedetection.notification_format,
        "notification_muted": False,
    }
