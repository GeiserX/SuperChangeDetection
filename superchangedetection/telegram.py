"""The Telegram Bot API, the part the bot uses: long polling, messages with HTML and a keyboard,
callback answers, chat actions, the menu and the descriptions.

The token sits in every request URL, so no error raised here ever quotes a URL: `TelegramError`
carries the method, Telegram's error code and its description, with any token-shaped string
replaced. 429 waits what Telegram says and tries again; everything else raises at once.
"""

from __future__ import annotations

import html
import re
import time

import httpx

API = "https://api.telegram.org"
_TOKEN = re.compile(r"\d{3,}:[A-Za-z0-9_-]{20,}")
MAX_RETRY_AFTER = 60
RETRIES_429 = 2
HTTP_MARGIN_S = 15  # on top of a long poll's own timeout


class TelegramError(Exception):
    """A failed Bot API call. `code` is Telegram's error code, 0 for a network failure."""

    def __init__(self, method: str, code: int, description: str, parameters: dict | None = None):
        self.method = method
        self.code = code
        self.description = _TOKEN.sub("<token>", description)
        self.parameters = parameters or {}
        super().__init__(f"{method}: {code} {self.description}")

    @property
    def retry_after(self) -> int | None:
        v = self.parameters.get("retry_after")
        return int(v) if isinstance(v, int | float) else None

    @property
    def migrate_to_chat_id(self) -> int | None:
        v = self.parameters.get("migrate_to_chat_id")
        return int(v) if isinstance(v, int) else None


def safe(e: BaseException) -> str:
    """An exception as text fit for the service log: our own exceptions keep their text, anything
    else keeps its class and HTTP status only, because a library's message may quote a URL."""
    name, module = type(e).__name__, type(e).__module__
    if module == "superchangedetection" or module.startswith("superchangedetection."):
        text = f"{name}: {e}"
    else:
        status = getattr(e, "code", None)
        if not isinstance(status, int):
            status = getattr(getattr(e, "response", None), "status_code", None)
        text = f"{name} {status}" if isinstance(status, int) else name
    return _TOKEN.sub("<token>", text)


class Telegram:
    def __init__(self, token: str, base: str = API, client: httpx.Client | None = None):
        self._url = f"{base}/bot{token.strip()}/"
        self._files = f"{base}/file/bot{token.strip()}/"
        self.c = client or httpx.Client()

    def __repr__(self) -> str:
        return "Telegram()"

    def call(self, method: str, **params):
        params = {k: v for k, v in params.items() if v is not None}
        timeout = int(params.get("timeout") or 0) + HTTP_MARGIN_S
        for attempt in range(RETRIES_429 + 1):
            payload = self._post(method, params, timeout)
            if payload.get("ok"):
                return payload.get("result")
            err = TelegramError(
                method,
                int(payload.get("error_code") or 0),
                str(payload.get("description") or ""),
                payload.get("parameters") if isinstance(payload.get("parameters"), dict) else None,
            )
            if err.code == 429 and attempt < RETRIES_429:
                time.sleep(min(err.retry_after or 1, MAX_RETRY_AFTER))
                continue
            raise err
        raise AssertionError("unreachable")  # pragma: no cover

    def _post(self, method: str, params: dict, timeout: float) -> dict:
        failure = None
        try:
            r = self.c.post(self._url + method, json=params, timeout=timeout)
        except httpx.HTTPError as e:  # its text may quote the URL: keep the class only
            failure = type(e).__name__
        if failure is not None:
            raise TelegramError(method, 0, failure)
        try:
            payload = r.json()
        except ValueError:
            payload = None
        if not isinstance(payload, dict):
            return {
                "ok": False,
                "error_code": r.status_code,
                "description": f"HTTP {r.status_code}",
            }
        return payload

    # ---- what the bot uses
    def get_me(self) -> dict:
        return self.call("getMe")

    def get_updates(self, offset: int | None, timeout: int) -> list[dict]:
        return self.call(
            "getUpdates",
            offset=offset,
            timeout=timeout,
            allowed_updates=["message", "callback_query"],
        )

    def send(
        self,
        chat_id: int,
        text: str,
        reply_to: int | None = None,
        markup: dict | None = None,
        html_mode: bool = True,
    ) -> dict:
        """One message. HTML that Telegram refuses is sent again as plain text, escaped."""
        params = {
            "chat_id": chat_id,
            "text": text,
            "reply_markup": markup,
            "disable_web_page_preview": True,
        }
        if reply_to:
            params["reply_parameters"] = {
                "message_id": reply_to,
                "allow_sending_without_reply": True,
            }
        if not html_mode:
            return self.call("sendMessage", **params)
        try:
            return self.call("sendMessage", parse_mode="HTML", **params)
        except TelegramError as e:
            if e.code != 400 or "parse" not in e.description.lower():
                raise
            return self.call("sendMessage", **{**params, "text": html.unescape(_strip_tags(text))})

    def edit_markup(self, chat_id: int, message_id: int, markup: dict | None) -> None:
        self.call(
            "editMessageReplyMarkup", chat_id=chat_id, message_id=message_id, reply_markup=markup
        )

    def answer_callback(self, callback_id: str, text: str | None = None) -> None:
        self.call("answerCallbackQuery", callback_query_id=callback_id, text=text)

    def typing(self, chat_id: int) -> None:
        self.call("sendChatAction", chat_id=chat_id, action="typing")

    def get_file(self, file_id: str) -> dict:
        return self.call("getFile", file_id=file_id)

    def download(self, file_path: str, timeout: float = 120) -> bytes:
        """The bytes of a file Telegram holds (`file_path` from getFile). The URL carries the
        token, so a failure keeps the exception's class only."""
        try:
            r = self.c.get(self._files + file_path, timeout=timeout)
        except httpx.HTTPError as e:
            raise TelegramError("download", 0, type(e).__name__) from None
        if r.status_code >= 400:
            raise TelegramError("download", r.status_code, f"HTTP {r.status_code}")
        return r.content

    def get_chat_member(self, chat_id: int, user_id: int) -> dict:
        return self.call("getChatMember", chat_id=chat_id, user_id=user_id)

    def set_profile(self, lang: str, commands: dict[str, str], description: str, short: str):
        self.call(
            "setMyCommands",
            commands=[{"command": k, "description": v} for k, v in commands.items()],
            language_code=lang,
        )
        self.call("setMyDescription", description=description[:512], language_code=lang)
        self.call("setMyShortDescription", short_description=short[:120], language_code=lang)


_TAG = re.compile(r"</?(b|i|s|u|code|pre|a)(\s[^>]*)?>")


def _strip_tags(text: str) -> str:
    return _TAG.sub("", text)
