"""The changedetection.io API (v1), the part the bot uses.

Every call is one HTTP request with the instance's API key. The instance is the source of truth:
the bot keeps no copy of the watches. A watch the bot created carries the chat it notifies in its
notification URL, which is how the bot tells its own watches from the others on the instance.
"""

from __future__ import annotations

import re
import time

import httpx

TGRAM = re.compile(r"^tgram://(?P<token>[^/]+)/(?P<chat>-?\d+)/?")


class CDError(Exception):
    pass


class CD:
    def __init__(self, url: str, api_key: str, timeout: float = 30):
        self.url = url.rstrip("/") + "/api/v1"
        self.c = httpx.Client(timeout=timeout, headers={"x-api-key": api_key})

    def _r(self, method: str, path: str, **kw) -> httpx.Response:
        try:
            r = self.c.request(method, self.url + path, **kw)
        except httpx.HTTPError as e:
            raise CDError(f"changedetection unreachable: {e.__class__.__name__}") from e
        if r.status_code >= 400:
            raise CDError(f"{method} {path}: {r.status_code} {r.text[:300]}")
        return r

    # ---- watches
    def watches(self) -> dict[str, dict]:
        return self._r("GET", "/watch").json()

    def watch(self, uuid: str) -> dict:
        return self._r("GET", f"/watch/{uuid}").json()

    def create(self, fields: dict) -> str:
        r = self._r("POST", "/watch", json=fields)
        data = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
        uuid = data.get("uuid") if isinstance(data, dict) else None
        return uuid or self._find(fields["url"])

    def _find(self, url: str) -> str:
        newest = max(self.watches().items(), key=lambda kv: kv[1].get("date_created") or 0)
        if newest[1].get("url") != url:
            raise CDError("created watch not found")
        return newest[0]

    def update(self, uuid: str, fields: dict) -> None:
        self._r("PUT", f"/watch/{uuid}", json=fields)

    def delete(self, uuid: str) -> None:
        self._r("DELETE", f"/watch/{uuid}")

    def recheck(self, uuid: str) -> None:
        self._r("GET", f"/watch/{uuid}", params={"recheck": 1})

    def history(self, uuid: str) -> dict[str, str]:
        return self._r("GET", f"/watch/{uuid}/history").json()

    def snapshot(self, uuid: str, timestamp: str = "latest", html: bool = False) -> str:
        params = {"html": 1} if html else None
        r = self.c.get(f"{self.url}/watch/{uuid}/history/{timestamp}", params=params)
        if r.status_code == 404:
            return ""
        if r.status_code >= 400:
            raise CDError(f"snapshot {uuid}: {r.status_code}")
        return r.text

    def diff(self, uuid: str) -> str:
        """The last change: the difference between the two newest snapshots, plain text."""
        stamps = sorted(self.history(uuid), key=int)
        if len(stamps) < 2:
            return ""
        r = self.c.get(
            f"{self.url}/watch/{uuid}/difference/{stamps[-2]}/{stamps[-1]}",
            params={"format": "text"},
        )
        return r.text if r.status_code == 200 else ""

    def wait_checked(self, uuid: str, since: int, seconds: int) -> dict:
        """Poll until the watch has been checked after `since`, or give up. Returns the watch."""
        deadline = time.time() + seconds
        while True:
            w = self.watch(uuid)
            if int(w.get("last_checked") or 0) > since:
                return w
            if time.time() >= deadline:
                return w
            time.sleep(3)

    # ---- tags
    def tags(self) -> dict[str, dict]:
        return self._r("GET", "/tags").json()

    def tag_uuid(self, name: str) -> str:
        for uuid, t in self.tags().items():
            if (t.get("title") or "").strip().lower() == name.strip().lower():
                return uuid
        r = self._r("POST", "/tag", json={"title": name})
        data = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
        uuid = data.get("uuid") if isinstance(data, dict) else None
        if uuid:
            return uuid
        for uuid, t in self.tags().items():
            if (t.get("title") or "").strip().lower() == name.strip().lower():
                return uuid
        raise CDError(f"tag {name!r} not created")

    def systeminfo(self) -> dict:
        return self._r("GET", "/systeminfo").json()


def notifies(watch: dict, token: str) -> int | None:
    """The Telegram chat this watch notifies through our bot, or None."""
    for u in watch.get("notification_urls") or []:
        m = TGRAM.match(u)
        if m and m.group("token") == token:
            return int(m.group("chat"))
    return None


def tgram_url(token: str, chat_id: int) -> str:
    return f"tgram://{token}/{chat_id}/"
