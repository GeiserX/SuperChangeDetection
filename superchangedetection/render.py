"""Turn action results into the message the chat reads."""

from __future__ import annotations

import html
import re

from .actions import Result
from .i18n import Translator


def results(rs: list[Result], t: Translator) -> str:
    parts = []
    for r in rs:
        if r.failed:
            parts.append(t("result_failed", op=r.op, error=r.text))
            continue
        seen = t("result_seen_pending") if r.pending else r.text or t("result_seen_empty")
        key = f"result_{r.op}_{'ok' if r.ok else 'error'}"
        if r.op == "delete":
            key = "result_delete_ok"
        parts.append(t(key, title=r.title or r.url, url=r.url, seen=seen))
    if any(r.op in ("create", "update") and r.ok for r in rs):
        parts.append(t("result_footer"))
    return "\n\n".join(parts)


# ---- the chat: Markdown from the AI as Telegram HTML, in pieces Telegram accepts


def chunks(text: str, size: int = 3500):
    """Pieces of at most `size` characters, cut at a line break when there is one. A code block
    cut in two is closed at the cut and opened again in the next piece."""
    floor = 0
    room = size - 4
    while len(text) > size:
        cut = text.rfind("\n", floor + 1, room)
        if cut <= floor:
            cut = room
        head, text = text[:cut], text[cut:].lstrip("\n")
        fence = _open_fence(head)
        floor = 0
        if fence is not None:
            head += "\n```"
            text = f"{fence}\n{text}"
            floor = len(fence)
        yield head
    if text:
        yield text


def _open_fence(text: str) -> str | None:
    fence = None
    for line in text.split("\n"):
        if line.lstrip().startswith("```"):
            fence = None if fence is not None else line.strip()[:64] or "```"
    return fence


def to_html(md: str) -> str:
    """Bold, italic, strike, code, links, headings and lists as Telegram HTML. Everything else is
    escaped, so the text can never break out of the markup."""
    kept: list[str] = []

    def stash(markup: str) -> str:
        kept.append(markup)
        return f"\x00{len(kept) - 1}\x00"

    def inline(s: str) -> str:
        body = r"((?:[^<\n]|<([bs])>[^<\n]*</\2>)+?)"
        s = re.sub(r"\*\*(?=\S)" + body + r"(?<=\S)\*\*", r"<b>\1</b>", s)
        s = re.sub(r"(?<!\w)__(?=\S)" + body + r"(?<=\S)__(?!\w)", r"<b>\1</b>", s)
        s = re.sub(r"~~(?=\S)" + body + r"(?<=\S)~~", r"<s>\1</s>", s)
        s = re.sub(r"(?<![\w*])\*(?=[^\s*])" + body + r"(?<=[^\s*])\*(?![\w*])", r"<i>\1</i>", s)
        return re.sub(r"(?<!\w)_(?=[^\s_])" + body + r"(?<=[^\s_])_(?!\w)", r"<i>\1</i>", s)

    s = md.replace("\x00", "")
    s = re.sub(
        r"```[^\n`]*\n?(.*?)```",
        lambda m: stash(f"<pre>{html.escape(m.group(1).strip(chr(10)), quote=False)}</pre>"),
        s,
        flags=re.DOTALL,
    )
    s = re.sub(r"`([^`\n]+)`", lambda m: stash(f"<code>{html.escape(m.group(1), False)}</code>"), s)
    s = re.sub(
        r"\[([^\]\n]+)\]\((https?://(?:[^()\s\x00]|\([^()\s\x00]*\))+)\)",
        lambda m: stash(
            f'<a href="{html.escape(m.group(2), quote=True)}">'
            f"{inline(html.escape(m.group(1), quote=False))}</a>"
        ),
        s,
    )
    s = html.escape(s, quote=False)
    s = re.sub(r"(?m)^#{1,6}[ \t]+(.+?)[ \t#]*$", r"<b>\1</b>", s)
    s = re.sub(r"(?m)^([ \t]*)[-*+][ \t]+", r"\1• ", s)
    s = inline(s)
    while "\x00" in s:
        s = re.sub(r"\x00(\d+)\x00", lambda m: kept[int(m.group(1))], s)
    return s
