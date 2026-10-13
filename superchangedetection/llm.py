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

Voice notes (settings.yaml, `transcription`): openai is any /audio/transcriptions endpoint; plugin
is a file defining transcribe(audio: bytes, filename: str, settings: dict) -> str.

openai and anthropic get the rules and context.md as the system prompt, the files inline after it,
and history plus new as the messages. They keep nothing between turns.
"""

from __future__ import annotations

import importlib.util
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
            return Reply(
                str(out.get("text", "")).strip(),
                str(conv) if conv else None,
                bool(out.get("kept", True)),
                _proposals(out.get("proposals")),
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
    "can_act",
    "complete",
    "converse",
    "forget",
    "transcribe",
]
