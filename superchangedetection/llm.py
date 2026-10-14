"""Talk to whatever language model the instance chose (settings.yaml, `llm`).

Providers:
- openai: any OpenAI-compatible chat endpoint (base_url, model; the key from api_key_env).
- anthropic: the Anthropic Messages API (model, key).
- plugin: a Python file under the data directory. It defines
      def converse(turn: dict, settings: dict) -> str | dict
      def forget(conversation: str, settings: dict) -> None      # optional, on /new
  turn is {"conversation": the id it returned last time in this chat, or None,
           "rules": the AI's rules (`scd rules`), "context": context.md as of now,
           "files": {"pages/1.html": ..., "snapshots/<uuid>.txt": ..., "diffs/<uuid>.txt": ...},
           "new": the lines it has not seen yet, [{"role": "user"|"app", "content": str}, ...]
                  ("app" lines report what the app did with the AI's last actions),
           "history": the earlier lines, a recap for when it has to start over}.
  It returns the answer, or {"text": answer, "conversation": id or None, "kept": bool}. The app
  keeps the id per chat and sends it back with the next message; None starts over with the recap
  next time, and kept False (the provider did not store this turn) sends these lines again.
  A plugin that keeps a conversation on its side sends only `new` each turn.

  Proposals: the app sets turn["proposals"] = True when the plugin also defines
      def act(proposal: dict, settings: dict) -> str | dict
  Then the answer dict may carry "proposals": [{"id": str, "summary": str, "confirm_by": "none" |
  "asker" | "admins", ...}, ...], things the plugin would do outside the chat (change the bot's own
  code, for example). The app shows each summary; with "none" it calls act() at once in the
  background, with "asker" after a yes from the person who asked, with "admins" after a yes from
  one of bot.users. Only bot.users may ask for a proposal. act() returns the text to post, or
  {"text": ..., "proposals": [...]} with follow-ups, offered the same way.

  One answer per burst: every line of `new` and `history` also carries "ts" (when it was written,
  ISO 8601 UTC), and the app sets
      turn["incoming"]() -> [{"id": int, "role": "user", "content": str, "ts": str}, ...]
  which the plugin may call from its thread while it works: each call hands over the lines that
  called the bot since the last call (an edit of a line of this turn comes as a correction in the
  same shape). The answer dict then names the ones it took into account, "covers": [ids]; every
  handed-over line it does not name goes with the next turn. A plugin that never calls incoming()
  covers nothing extra, and those lines make the next turn.

Buttons and reminders: an answer may end with blocks the app turns into buttons, for every provider
(the rules teach them, context.RULES): ```options (a choice: "- label" lines), ```confirm (a yes or
no question, no lines), ```offer (something the assistant could do: "- label" lines, Yes and No when
none), and ```reminders ("- YYYY-MM-DD HH:MM | text | watch number" lines, the watch optional). The
app takes them out of the text. A plugin may instead return them beside the text, and these win:
"buttons": {"kind": "options" | "confirm" | "offer", "labels": [str, ...]} and
"reminders": [{"when": "YYYY-MM-DD HH:MM", "text": str, "watch": str | None}, ...]. A tap is that
person's next message; an untapped offer expires and nothing is done.

Voice notes (settings.yaml, `transcription`): openai is any /audio/transcriptions endpoint; plugin
is a file defining transcribe(audio: bytes, filename: str, settings: dict) -> str.

openai and anthropic get the rules and context.md as the system prompt, the files inline after it,
and history plus new as the messages. They keep nothing between turns.
"""

from __future__ import annotations

import importlib.util
import re
import sys
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from types import ModuleType

import httpx

from .model import Data


class LLMError(Exception):
    pass


class NoAct(LLMError):
    """The provider has no act(), so it cannot carry out proposals."""


CONFIRM_BY = ("none", "asker", "admins")


@dataclass
class Reply:
    text: str
    conversation: str | None = None
    kept: bool = True
    proposals: list[dict] = field(default_factory=list)
    covers: set[int] = field(default_factory=set)  # handed-over lines the answer took in
    buttons: dict | None = None  # {"kind": options | confirm | offer, "labels": [...]}
    reminders: list[dict] = field(default_factory=list)  # {"when", "text", "watch"} to offer


BUTTON_KINDS = ("options", "confirm", "offer")
LABELS_MAX, LABEL_CHARS = 5, 40
BUTTONS = re.compile(r"\n?```(options|confirm|offer)[ \t]*\n(.*?)```[ \t]*", re.DOTALL)
REMINDERS_MAX, REMINDER_CHARS = 5, 300
REMINDERS = re.compile(r"\n?```reminders[ \t]*\n(.*?)```[ \t]*", re.DOTALL)
WHEN = re.compile(r"\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}")


def buttons(kind, labels) -> dict | None:
    """Well-formed buttons: a known kind and short, distinct labels (at most LABELS_MAX). A confirm
    takes no labels (the app shows yes and no); options need at least one; an offer with none gets
    yes and no. Anything else is None."""
    if kind not in BUTTON_KINDS:
        return None
    out: list[str] = []
    for x in labels if isinstance(labels, list) else []:
        label = " ".join(str(x).split()) if isinstance(x, str | int | float) else ""
        if label and len(label) <= LABEL_CHARS and label not in out:
            out.append(label)
    if kind == "confirm":
        return {"kind": kind, "labels": []}
    if kind == "options" and not out:
        return None
    return {"kind": kind, "labels": out[:LABELS_MAX]}


def extract_buttons(text: str) -> tuple[str, dict | None]:
    """The answer without its buttons blocks, and the buttons of the last one."""
    blocks = list(BUTTONS.finditer(text or ""))
    if not blocks:
        return text or "", None
    kind, body = blocks[-1].group(1), blocks[-1].group(2)
    lines = [x.strip() for x in body.splitlines()]
    labels = [x[1:].strip() for x in lines if x.startswith(("-", "*"))]
    return BUTTONS.sub("", text).strip(), buttons(kind, labels)


def reminders(raw) -> list[dict]:
    """Well-formed reminders: a time written as YYYY-MM-DD HH:MM, a short text, a watch (its number
    or uuid) or None; at most REMINDERS_MAX, anything else is dropped. Whether the time is still to
    come is the app's to judge when it offers them."""
    out: list[dict] = []
    for r in raw if isinstance(raw, list) else []:
        if not isinstance(r, dict):
            continue
        when = str(r.get("when") or "").strip()
        text = " ".join(str(r.get("text") or "").split())
        watch = r.get("watch")
        watch = str(watch).strip() if isinstance(watch, str | int) and str(watch).strip() else None
        if not WHEN.fullmatch(when) or not text or len(text) > REMINDER_CHARS:
            continue
        item = {"when": when.replace("T", " "), "text": text, "watch": watch}
        if all((o["when"], o["text"]) != (item["when"], item["text"]) for o in out):
            out.append(item)
    return out[:REMINDERS_MAX]


def extract_reminders(text: str) -> tuple[str, list[dict]]:
    """The answer without its reminders block, and the reminders it lists."""
    blocks = list(REMINDERS.finditer(text or ""))
    if not blocks:
        return text or "", []
    found = []
    for line in blocks[-1].group(1).splitlines():
        line = line.strip()
        if not line.startswith(("-", "*")):
            continue
        parts = [x.strip() for x in line[1:].split("|")]
        if len(parts) >= 2:
            found.append(
                {"when": parts[0], "text": parts[1], "watch": parts[2] if len(parts) > 2 else None}
            )
    return REMINDERS.sub("", text).strip(), reminders(found)


def can_act(data: Data) -> bool:
    """Whether the provider can carry out proposals: a plugin with act()."""
    if data.settings.llm.provider != "plugin":
        return False
    try:
        return _function(data, "act", required=False) is not None
    except LLMError:
        return False


def _proposals(raw) -> list[dict]:
    """The well-formed proposals in a plugin's answer; anything else is dropped."""
    out = []
    for p in raw if isinstance(raw, list) else []:
        if (
            isinstance(p, dict)
            and isinstance(p.get("id"), str)
            and p["id"]
            and isinstance(p.get("summary"), str)
            and p["summary"].strip()
            and p.get("confirm_by") in CONFIRM_BY
        ):
            out.append({**p, "summary": p["summary"].strip()})
    return out


def converse(data: Data, turn: dict) -> Reply:
    cfg = data.settings.llm
    if cfg.provider == "none":
        raise LLMError("no provider configured")
    if cfg.provider == "plugin":
        fn = _function(data, "converse")
        out = fn({**turn, "proposals": can_act(data)}, _settings(data))
        if isinstance(out, dict):
            conv = out.get("conversation")
            covers = out.get("covers")
            b = out.get("buttons")
            return Reply(
                str(out.get("text", "")).strip(),
                str(conv) if conv else None,
                bool(out.get("kept", True)),
                _proposals(out.get("proposals")),
                {i for i in covers if type(i) is int} if isinstance(covers, list) else set(),
                buttons(b.get("kind"), b.get("labels")) if isinstance(b, dict) else None,
                reminders(out.get("reminders")),
            )
        return Reply(str(out).strip())
    system = f"{turn['rules']}\n\n{turn['context']}"
    for name, body in turn.get("files", {}).items():
        system += f'\n\n<file path="{name}">\n{body}\n</file>'
    messages = _merge([*turn.get("history", []), *turn["new"]])
    if not messages or messages[0]["role"] != "user":
        messages.insert(0, {"role": "user", "content": "(conversation start)"})
    return Reply(complete(data, system, messages))


def forget(data: Data, conversation: str) -> None:
    """Tell a plugin that keeps conversations to drop one; /new already started over."""
    if data.settings.llm.provider == "plugin":
        fn = _function(data, "forget", required=False)
        if fn is not None:
            fn(conversation, _settings(data))


def act(data: Data, proposal: dict) -> Reply:
    """Carry out a proposal the plugin made, once the right person said yes (or at once, for
    confirm_by none). The text to post, and any follow-up proposals."""
    fn = _function(data, "act", required=False) if data.settings.llm.provider == "plugin" else None
    if fn is None:
        raise NoAct("the provider cannot carry out proposals")
    out = fn(proposal, _settings(data))
    if isinstance(out, dict):
        return Reply(str(out.get("text", "")).strip(), proposals=_proposals(out.get("proposals")))
    return Reply(str(out).strip())


def transcribe(data: Data, audio: bytes, filename: str) -> str:
    """The text of a voice note, through the configured transcription provider."""
    cfg = data.settings.transcription
    if cfg.provider == "none":
        raise LLMError("no transcription provider configured")
    if cfg.provider == "plugin":
        fn = _function(data, "transcribe", plugin=cfg.plugin)
        settings = {k: v for k, v in cfg.model_dump().items() if k != "plugin"}
        return str(fn(audio, filename, settings)).strip()
    if cfg.provider == "openai":
        base = (cfg.base_url or "https://api.openai.com/v1").rstrip("/")
        key = data.secret(cfg.api_key_env)
        headers = {"Authorization": f"Bearer {key}"} if key else {}
        form = {"model": cfg.model}
        if cfg.language:
            form["language"] = cfg.language
        try:
            r = httpx.post(
                f"{base}/audio/transcriptions",
                data=form,
                files={"file": (filename, audio)},
                headers=headers,
                timeout=cfg.timeout_s,
            )
        except httpx.HTTPError as e:
            raise LLMError(type(e).__name__) from None
        if r.status_code >= 400:
            raise LLMError(f"HTTP {r.status_code}")
        return str(r.json().get("text", "")).strip()
    raise LLMError(f"unknown transcription provider {cfg.provider}")


def complete(data: Data, system: str, messages: list[dict]) -> str:
    cfg = data.settings.llm
    key = data.secret(cfg.api_key_env)
    timeout = cfg.timeout_s
    if cfg.provider == "openai":
        base = (cfg.base_url or "https://api.openai.com/v1").rstrip("/")
        headers = {"Authorization": f"Bearer {key}"} if key else {}
        body = {
            "model": cfg.model,
            "max_tokens": cfg.max_tokens,
            "messages": [{"role": "system", "content": system}, *messages],
        }
        r = _post(f"{base}/chat/completions", body, headers, timeout)
        return r["choices"][0]["message"]["content"].strip()
    if cfg.provider == "anthropic":
        base = (cfg.base_url or "https://api.anthropic.com").rstrip("/")
        headers = {"x-api-key": key, "anthropic-version": "2023-06-01"}
        body = {
            "model": cfg.model,
            "max_tokens": cfg.max_tokens,
            "system": system,
            "messages": messages,
        }
        r = _post(f"{base}/v1/messages", body, headers, timeout)
        return "".join(b.get("text", "") for b in r["content"]).strip()
    raise LLMError(f"provider {cfg.provider} cannot complete")


def _post(url: str, body: dict, headers: dict, timeout: float) -> dict:
    try:
        r = httpx.post(url, json=body, headers=headers, timeout=timeout)
    except httpx.HTTPError as e:
        raise LLMError(type(e).__name__) from None
    if r.status_code >= 400:
        raise LLMError(f"HTTP {r.status_code}")
    return r.json()


def _merge(messages: list[dict]) -> list[dict]:
    """Consecutive lines of one role joined; the app's notes ride as user content."""
    out: list[dict] = []
    for m in messages:
        role = "assistant" if m["role"] == "assistant" else "user"
        content = m["content"] if m["role"] != "app" else f"[app] {m['content']}"
        if out and out[-1]["role"] == role:
            out[-1] = {"role": role, "content": out[-1]["content"] + "\n" + content}
        else:
            out.append({"role": role, "content": content})
    return out


def _settings(data: Data) -> dict:
    return {
        k: v
        for k, v in data.settings.llm.model_dump().items()
        if k not in ("plugin", "instructions")
    }


@lru_cache(maxsize=8)
def _module(path: str, mtime_ns: int) -> ModuleType:
    """One module per plugin file version: state it keeps survives between calls, and an edited
    file is loaded again. Registered in sys.modules first, as a normal import is."""
    name = f"scd_plugin_{abs(hash(path))}_{mtime_ns}"
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise LLMError("plugin cannot be loaded")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    try:
        spec.loader.exec_module(mod)
    except BaseException:
        sys.modules.pop(name, None)
        raise
    return mod


def _function(data: Data, name: str, required: bool = True, plugin: str | None = ...):
    rel = data.settings.llm.plugin if plugin is ... else plugin
    if not rel:
        raise LLMError("a plugin provider needs a .py file under the data directory")
    path = (data.root / rel).resolve()
    if data.root.resolve() not in path.parents:
        raise LLMError("a plugin must live inside the data directory")
    try:
        mod = _module(str(path), path.stat().st_mtime_ns)
    except FileNotFoundError:
        raise LLMError(f"{rel} does not exist") from None
    fn = getattr(mod, name, None)
    if not callable(fn):
        if required:
            raise LLMError(f"{rel} defines no {name}()")
        return None
    return fn


__all__ = [
    "LLMError",
    "NoAct",
    "Path",
    "Reply",
    "act",
    "buttons",
    "can_act",
    "complete",
    "converse",
    "extract_buttons",
    "extract_reminders",
    "forget",
    "reminders",
    "transcribe",
]
