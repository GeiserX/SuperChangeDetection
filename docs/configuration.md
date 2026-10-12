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
| `users` | `[]` | Telegram user ids that may also talk to the bot in private |
| `handles` | `{}` | user id (as a string) to `@handle`; looked up from Telegram when left out |
| `history` | `20` | recent messages kept as context for the AI |
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

## `fetch`

| key | default | meaning |
|---|---|---|
| `enabled` | `true` | fetch the HTML of every URL in a message and give it to the AI |
| `max_kb` | `400` | cap per page after cleaning |
| `timeout_s` | `20` | HTTP timeout |
| `user_agent` | a desktop Chrome string | sent with the fetch |
