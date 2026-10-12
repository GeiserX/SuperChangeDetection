"""settings.yaml: everything an instance decides. Secrets come from the environment only."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field


class Loose(BaseModel):
    model_config = ConfigDict(extra="allow")


class Bot(Loose):
    token_env: str = "TELEGRAM_BOT_TOKEN"
    chat_id: int | None = None  # the group; anyone in it may use the bot
    users: list[int] = []  # Telegram user ids that may also talk to the bot in private
    handles: dict[
        str, str
    ] = {}  # user id (as a string) -> @handle, looked up from Telegram if left out
    history: int = 20  # recent messages kept as context for the AI
    sync_profile: bool = True  # at start, set the bot's command menu and descriptions in Telegram


class LLM(Loose):
    provider: Literal["none", "openai", "anthropic", "plugin"] = "none"
    model: str = ""
    base_url: str = ""  # openai: any OpenAI-compatible endpoint
    api_key_env: str = "LLM_API_KEY"
    plugin: str | None = None  # provider "plugin": a .py file under the data dir
    max_tokens: int = 1500
    timeout_s: int = 600
    instructions: str = ""  # extra instructions for this instance, in any language


class Interval(Loose):
    weeks: int = 0
    days: int = 0
    hours: int = 1
    minutes: int = 0
    seconds: int = 0


class ChangeDetection(Loose):
    url: str = "http://changedetection:5000"
    api_key_env: str = "CD_API_KEY"
    default_interval: Interval = Interval()
    fetch_backend: str = "system"  # system | html_requests | html_webdriver
    tag: str | None = None  # a changedetection tag (group) every watch the bot creates gets
    notification_format: Literal["text", "html", "markdown"] = "text"
    first_check_wait_s: int = 90  # how long to wait for the first snapshot after a create/update
    timeout_s: int = 30


class Fetch(Loose):
    enabled: bool = True  # give the AI the page's HTML when a message holds a URL
    max_kb: int = 400
    timeout_s: int = 20
    user_agent: str = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0 Safari/537.36"
    )


class Settings(Loose):
    locale: str = "en"
    timezone: str = "UTC"
    bot: Bot = Bot()
    llm: LLM = LLM()
    changedetection: ChangeDetection = ChangeDetection()
    fetch: Fetch = Fetch()
    snapshot_chars: int = Field(
        default=12000, ge=500
    )  # of each watch's latest text given to the AI
    diff_chars: int = Field(default=4000, ge=200)  # of each watch's last diff given to the AI


class Data:
    """The data directory: settings.yaml, an optional plugins/ folder and state/ (ours)."""

    def __init__(self, root: Path | None = None):
        self.root = Path(root or os.environ.get("SCD_DATA", "data"))
        path = self.root / "settings.yaml"
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) if path.exists() else {}
        self.settings = Settings.model_validate(raw or {})
        self.state = self.root / "state"

    def secret(self, env: str) -> str:
        return os.environ.get(env, "")
