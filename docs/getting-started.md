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
    image: drumsergio/superchangedetection:0.5.0
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

`scd run` (the Docker image's default command) long-polls Telegram. Only one process may poll a bot
token at a time; the other `scd` commands do not poll, so they are safe beside the running bot.

1. Add the bot to the group and send `/chatid` there: the answer is the `bot.chat_id` for
   `settings.yaml`. Put the user ids that may talk to it in private in `bot.users`.
2. Make the bot an admin of the group, or turn privacy mode off in BotFather. With privacy mode on and
   no admin rights, Telegram delivers commands and replies to the bot but not the messages that
   mention it; the bot logs a warning at start when that is the case.
3. At start the bot sets its command menu and descriptions in Telegram from the locale file
   (`bot.sync_profile`), so BotFather's `/setcommands` is not needed.

Then write to it: «avísame cuando cambie el precio de https://...». What it does with the message is in
[Usage](usage.md); what the AI sees and may do, in [How it works](how-it-works.md).
