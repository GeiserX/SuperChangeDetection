import json
import time

import httpx
import pytest

from superchangedetection.cd import CD

TOKEN = "123:abc"
U1 = "11111111-1111-4111-8111-111111111111"
U2 = "22222222-2222-4222-8222-222222222222"


class FakeInstance:
    """Just enough of the changedetection API for the tests: watches, tags, history, diff."""

    def __init__(self):
        self.watches = {
            U1: {
                "uuid": U1,
                "url": "https://a.example/x",
                "title": "A",
                "date_created": 10,
                "notification_urls": [f"tgram://{TOKEN}/-5/"],
                "last_checked": 100,
                "last_changed": 90,
                "include_filters": [".p"],
                "fetch_backend": "system",
                "time_between_check_use_default": True,
            },
            U2: {
                "uuid": U2,
                "url": "https://b.example/",
                "title": "B",
                "date_created": 20,
                "notification_urls": ["tgram://other:tok/-9/"],
                "last_checked": 100,
                "last_changed": 0,
            },
        }
        self.tags = {}
        self.snapshots = {U1: {"90": "price 10", "100": "price 12"}}
        self.calls = []
        self.now = int(time.time())

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix("/api/v1")
        self.calls.append((request.method, path, dict(request.url.params)))
        body = json.loads(request.content) if request.content else {}
        if path == "/systeminfo":
            return httpx.Response(200, json={"version": "0.60.8", "watch_count": len(self.watches)})
        if path == "/watch" and request.method == "GET":
            return httpx.Response(200, json=self.watches)
        if path == "/watch" and request.method == "POST":
            uuid = "33333333-3333-4333-8333-333333333333"
            self.now += 1
            self.watches[uuid] = {
                "uuid": uuid,
                "date_created": self.now,
                "last_checked": 0,
                "last_changed": 0,
                **body,
            }
            self.snapshots[uuid] = {str(self.now + 5): "new text"}
            self.watches[uuid]["last_checked"] = self.now + 5  # the first check has run
            return httpx.Response(200, json={"uuid": uuid})
        if path == "/tags":
            return httpx.Response(200, json=self.tags)
        if path == "/tag" and request.method == "POST":
            uuid = "tag-" + body["title"].lower()
            self.tags[uuid] = {"title": body["title"]}
            return httpx.Response(200, json={"uuid": uuid})
        parts = path.strip("/").split("/")
        if parts[0] == "watch" and len(parts) >= 2:
            uuid = parts[1]
            if uuid not in self.watches:
                return httpx.Response(404, text="not found")
            w = self.watches[uuid]
            if len(parts) == 2:
                if request.method == "GET":
                    if request.url.params.get("recheck"):
                        w["last_checked"] = self.now + 50
                        return httpx.Response(200, text="OK")
                    return httpx.Response(200, json=w)
                if request.method == "PUT":
                    w.update(body)
                    return httpx.Response(200, text="OK")
                if request.method == "DELETE":
                    del self.watches[uuid]
                    return httpx.Response(204)
            if parts[2] == "history" and len(parts) == 3:
                return httpx.Response(
                    200, json={k: f"/{k}.txt" for k in self.snapshots.get(uuid, {})}
                )
            if parts[2] == "history":
                snaps = self.snapshots.get(uuid, {})
                if not snaps:
                    return httpx.Response(404, text="no history")
                key = max(snaps, key=int) if parts[3] == "latest" else parts[3]
                return httpx.Response(200, text=snaps[key])
            if parts[2] == "difference":
                return httpx.Response(
                    200,
                    text=f"-{self.snapshots[uuid][parts[3]]}\n+{self.snapshots[uuid][parts[4]]}",
                )
        return httpx.Response(404, text=f"unhandled {request.method} {path}")


@pytest.fixture
def instance():
    return FakeInstance()


@pytest.fixture
def cd(instance):
    c = CD("http://cd:5000", "key")
    c.c = httpx.Client(
        transport=httpx.MockTransport(instance.handler), headers={"x-api-key": "key"}
    )
    return c
