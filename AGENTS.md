# SuperChangeDetection: agent notes

Read this before changing anything.

- **What it is.** A Telegram bot for a changedetection.io instance. People say in the chat what to
  watch; the AI picks the page part and the filters and emits an actions block; the app validates
  and applies it through the changedetection API and shows what the watch sees now. Notifications
  go from changedetection straight to the chat (apprise `tgram://`), the bot is not involved.
- **Public repo.** Nothing private goes here: no hostnames, chat ids, user ids, tokens, people's
  names. The instance's own data lives in its `settings.yaml` outside the repo. The AI provider for
  a private deployment is a plugin file outside the repo too (`llm.provider: plugin`).
- **English code and docs; the chat speaks the instance's locale.** Every user-facing string lives
  in `superchangedetection/locales/<locale>.yaml`; `en.yaml` holds every key, the others mirror
  it. Spanish keeps its accents and opening marks (¿ ¡).
- **The instance is the source of truth.** The bot keeps no copy of the watches. A watch the bot
  owns is one whose notification URL names our bot token and a chat.
- **Only `actions.ALLOWED` reaches changedetection.** Notification fields are the app's; the AI
  never sets them. Adding a field means adding it there, with its type, and a test.
- **Tests:** `uv run --frozen pytest -q`; lint `uv run --frozen ruff check .` and `ruff format`.
  The fake instance in `tests/conftest.py` is the only changedetection the tests talk to.
- **Release:** bump `__version__`, tag `vX.Y.Z` on main; the Docker workflow publishes
  `drumsergio/superchangedetection:<version>` (amd64 and arm64).
