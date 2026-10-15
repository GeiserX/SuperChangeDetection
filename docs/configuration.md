# Configuration

Everything lives in `settings.yaml` under the data directory (`SCD_DATA`, default `data`). Secrets come
from the environment only. Defaults are the values shown.

## Top level

| key | default | meaning |
|---|---|---|
| `locale` | `en` | language of the chat and the notifications: a file in `superchangedetection/locales` |
| `timezone` | `UTC` | for the times shown to the AI and the chat |
| `snapshot_chars` | `12000` | how much of each watch's latest text the AI gets |
| `diff_chars` | `4000` | how much of each watch's last change the AI gets |

## `bot`

| key | default | meaning |
|---|---|---|
| `token_env` | `TELEGRAM_BOT_TOKEN` | env var holding the bot token |
| `chat_id` | none | the group; anyone in it may use the bot |
| `users` | `[]` | Telegram user ids that may also talk to the bot in private, and the only ones who may ask for a proposal |
| `handles` | `{}` | user id (as a string) to `@handle`; looked up from Telegram when left out |
| `history` | `20` | recent messages kept as context for the AI |
| `history_days` | `0` | and every message of these last days, for a provider that has to start over (`0`: only `history`) |
| `history_scope` | `asked` | `asked` or `all`: with `all`, group messages that do not call the bot go to the AI as context with its next turn; Telegram shows a bot those only when it is a group admin or privacy mode is off |
| `sync_profile` | `true` | set the bot's command menu and descriptions in Telegram at start |

## `changedetection`

| key | default | meaning |
|---|---|---|
| `url` | `http://changedetection:5000` | the instance, as reached from where the bot runs |
| `api_key_env` | `CD_API_KEY` | env var holding the API key |
| `default_interval` | `{hours: 1}` | interval of a new watch when the request does not say (`weeks`, `days`, `hours`, `minutes`, `seconds`) |
| `fetch_backend` | `system` | `system` (the instance default), `html_requests` (fast, no JavaScript) or `html_webdriver` (a real browser) |
| `tag` | none | a changedetection tag every watch the bot creates gets, created if missing |
| `notification_format` | `text` | `text`, `html` or `markdown`, passed to apprise |
| `first_check_wait_s` | `90` | how long to wait for the check after a create, update or recheck before answering |
| `timeout_s` | `30` | HTTP timeout to the instance |

## `llm`

| key | default | meaning |
|---|---|---|
| `provider` | `none` | `none`, `openai` (any OpenAI-compatible chat endpoint), `anthropic`, `plugin` |
| `model` | `""` | model name |
| `base_url` | `""` | openai: the endpoint (OpenAI, OpenRouter, Ollama's `/v1`, LM Studio, vLLM) |
| `api_key_env` | `LLM_API_KEY` | env var holding the key; may be unset for a local model |
| `plugin` | none | provider `plugin`: a `.py` file under the data directory |
| `max_tokens` | `1500` | answer budget |
| `timeout_s` | `600` | how long one answer may take |
| `instructions` | `""` | extra rules for this instance, appended to the AI's rules, in any language |

## `transcription`

Voice notes. With a provider, a voice note or audio file from someone allowed is always answered, in the
group too, with its caption if it has one.

| key | default | meaning |
|---|---|---|
| `provider` | `none` | `none`, `openai` (any `/audio/transcriptions` endpoint: OpenAI, faster-whisper-server), `plugin` |
| `base_url` | `""` | openai: the endpoint |
| `model` | `whisper-1` | model name |
| `api_key_env` | `STT_API_KEY` | env var holding the key |
| `language` | none | ISO code; unset lets the model detect it |
| `plugin` | none | provider `plugin`: a `.py` file under the data directory defining `transcribe(audio: bytes, filename: str, settings: dict) -> str` |
| `timeout_s` | `300` | how long one transcription may take |

### The plugin contract

`provider: plugin` loads the file named in `plugin` (it must live under the data directory) and calls:

```python
def converse(turn: dict, settings: dict) -> str | dict: ...  # required
def forget(conversation: str, settings: dict) -> None: ...  # optional, on /nuevo
```

`turn` has `conversation` (what the plugin returned last time in this chat, or `None`), `chat` (the
chat's Telegram id), `asker` (the Telegram user id of the person whose line started the turn, the oldest
that calls the bot, or `None`), `rules`
(`scd rules`), `context` (`context.md` as of now), `files` (`pages/<n>.html`, `snapshots/<uuid>.txt`,
`diffs/<uuid>.txt`), `new` (the lines the plugin has not seen yet, `[{"role": "user" | "app",
"content"}]`; an `app` line reports what the app did with the AI's last block), `history` (the earlier
lines, a recap for when it starts over) and `attachments` (the photos and files sent with the new lines,
`[{"name": "inbox/<message id>-<name>", "mime", "data": bytes, "from"}]`; the line that carries one names
it as `[<kind>: <name>]`, such as `[photo: <name>]` or `[file: <name>]`). The built-in `openai` and `anthropic` providers get only that
line, not the file. `settings` is the `llm` block minus `plugin` and `instructions`.
Return the answer, or `{"text", "conversation", "kept"}`: the app keeps `conversation` per chat and sends
it back next time; `kept: false` means the provider did not store this turn, so the lines go again with the
next one. The plugin must return before `timeout_s`. The answer may end with one `actions` block
([How it works](how-it-works.md)); the app validates it.

Every line of `new` and `history` also has `ts`, when it was written (ISO 8601, UTC). A line that starts
with `chat:` did not call the bot (`bot.history_scope: all`). `turn["incoming"]` is a function the plugin
may call from its thread while it works: each call returns the lines that called the bot since the last
call, `[{"id", "role": "user", "content", "ts"}]`, an edit of a line of this turn included as a
correction. The answer dict names the ones it took into account as `"covers": [ids]`; every line handed
over and not named goes with the next turn. A plugin that never calls `incoming()` gets those lines in the
next turn.

The answer may put buttons under it and offer reminders, as fenced blocks at the end of the text that
the app takes out (`scd rules` teaches them to the AI): `options`, `confirm`, `offer` and `reminders`
([How it works](how-it-works.md)). A plugin may return them beside the text instead, and these win:
`"buttons": {"kind": "options" | "confirm" | "offer", "labels": [...]}` and `"reminders": [{"when":
"YYYY-MM-DD HH:MM", "text", "watch": number or uuid or null}]`.

A plugin may also define

```python
def act(proposal: dict, settings: dict) -> str | dict: ...  # optional: carry out a proposal
```

Then the app sets `turn["proposals"] = True`, and the answer dict may carry `"proposals": [{"id",
"summary", "confirm_by": "none" | "asker" | "admins", ...}]`: things the plugin would do outside the chat
(open and merge a change to the bot's own code, for example). The app shows each `summary`; `none` runs
`act()` at once in the background, `asker` after a yes from the person who asked, `admins` after a yes from
one of `bot.users`. Only `bot.users` may ask for a proposal. `act()` gets the same dict and returns the text
to post, or `{"text", "proposals"}` with follow-ups, offered the same way. It may take up to an hour; the
chat is told if the bot restarts in the middle.

## `fetch`

| key | default | meaning |
|---|---|---|
| `enabled` | `true` | fetch the HTML of every URL in a message and give it to the AI |
| `max_kb` | `400` | cap per page after cleaning |
| `timeout_s` | `20` | HTTP timeout |
| `user_agent` | a desktop Chrome string | sent with the fetch |
