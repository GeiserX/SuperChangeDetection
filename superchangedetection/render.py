"""Turn action results into the message the chat reads."""

from __future__ import annotations

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
