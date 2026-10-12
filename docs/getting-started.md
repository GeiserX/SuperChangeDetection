# Getting started

## What you need

- A running changedetection.io instance (0.50 or newer) with its API key: Settings, API, "API access token".
- A Telegram bot from [@BotFather](https://t.me/BotFather): its token. For a group, make the bot an admin or
  turn privacy mode off (`/setprivacy`), or Telegram never delivers the group's mentions to it.
- An AI provider for the chat part: an OpenAI-compatible endpoint, Anthropic, or your own plugin file
  (see [Configuration](configuration.md)). Without one, only the command line works.

## Run it

Docker, next to changedetection:

```yaml
services:
  superchangedetection:
    image: drumsergio/superchangedetection:0.1.0
    restart: unless-stopped
    environment:
      TZ: Europe/Madrid
      CD_API_KEY: ${CD_API_KEY}
      TELEGRAM_BOT_TOKEN: ${TELEGRAM_BOT_TOKEN}
    volumes:
      - ./data:/data        # settings.yaml, plugins/, state/
```

`data/settings.yaml` starts from [settings.example.yaml](settings.example.yaml). The changedetection URL is
the one the container reaches, `http://changedetection:5000` when both share a Docker network.

From a checkout instead: `uv sync` then `uv run scd check`.

## First check

```bash
scd check          # the instance version and the watch count
scd watches        # every watch as the AI sees it
scd rules          # the AI's instructions, to read what it will and will not do
```

## The Telegram side

The chat part of the bot (long polling, mentions, replies, confirmations) is the next milestone; today
the same actions run from the command line with `scd apply` (see [Usage](usage.md)). Notifications
already work the way they will with the bot: every watch created through the app notifies the chat it
was created for, with the template from the locale file.
