"""Fetch a page for the AI: the HTML without scripts, styles and noise, capped in size, so the
session can read the real structure and write a selector that matches."""

from __future__ import annotations

import re

import httpx

URL = re.compile(r"https?://[^\s<>()\"']+")
_DROP = re.compile(
    r"<(script|style|noscript|svg|iframe|template|video|audio|canvas)\b.*?</\1\s*>",
    re.IGNORECASE | re.DOTALL,
)
_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)
_WS = re.compile(r"[ \t\r\f\v]+")
_BLANK = re.compile(r"\n\s*\n+")
_LONG_ATTR = re.compile(
    r"\s(?:style|srcset|data-[\w-]+|on\w+)=(?:\"[^\"]*\"|'[^']*')", re.IGNORECASE
)
_BASE64 = re.compile(r"data:[\w/+.-]+;base64,[A-Za-z0-9+/=]+")


def urls_in(text: str) -> list[str]:
    out = []
    for u in URL.findall(text or ""):
        u = u.rstrip(".,;:!?»")
        if u not in out:
            out.append(u)
    return out


def clean(html: str, max_kb: int = 400) -> str:
    html = _COMMENT.sub("", html)
    html = _DROP.sub("", html)
    html = _LONG_ATTR.sub("", html)
    html = _BASE64.sub("data:…", html)
    html = _WS.sub(" ", html)
    html = _BLANK.sub("\n", html)
    cap = max_kb * 1024
    if len(html.encode()) > cap:
        html = html.encode()[:cap].decode(errors="ignore") + "\n<!-- truncated -->"
    return html.strip()


def page(url: str, timeout: float = 20, user_agent: str = "", max_kb: int = 400) -> str:
    """The cleaned HTML, or a one-line note of why there is none (the AI reads either)."""
    headers = {"User-Agent": user_agent} if user_agent else {}
    try:
        r = httpx.get(url, headers=headers, timeout=timeout, follow_redirects=True)
    except httpx.HTTPError as e:
        return f"<!-- fetch failed: {e.__class__.__name__} -->"
    ctype = r.headers.get("content-type", "")
    if r.status_code >= 400:
        return f"<!-- fetch failed: HTTP {r.status_code} -->"
    if "html" not in ctype and "xml" not in ctype and "json" not in ctype:
        return f"<!-- not a text page: {ctype} -->"
    return clean(r.text, max_kb)
