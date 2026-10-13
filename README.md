<p align="center">
  <img src="docs/images/banner.svg" alt="SuperChangeDetection" width="100%">
</p>

<h1 align="center">SuperChangeDetection</h1>

<p align="center">
  <a href="https://github.com/GeiserX/SuperChangeDetection/releases"><img src="https://img.shields.io/github/v/release/GeiserX/SuperChangeDetection?style=flat-square" alt="Release"></a>
  <a href="https://github.com/GeiserX/SuperChangeDetection/actions/workflows/ci.yml"><img src="https://img.shields.io/github/actions/workflow/status/GeiserX/SuperChangeDetection/ci.yml?style=flat-square&label=CI" alt="CI"></a>
  <a href="LICENSE"><img src="https://img.shields.io/github/license/GeiserX/SuperChangeDetection?style=flat-square" alt="License"></a>
</p>

SuperChangeDetection is a Telegram bot for a self-hosted [changedetection.io](https://github.com/dgtlmoon/changedetection.io) instance. Say in the chat what you want watched, in your own words, and the bot reads the page, picks the part that matters, creates the watch and shows you what it sees. From then on changedetection tells the chat directly when the page changes; the bot only comes back when you mention it or reply to it, for example to say a watch fires too often or missed something.

## Features

- Create a watch from one chat message: the page's HTML is read and a CSS or XPath filter is chosen for you
- Fix a noisy or blind watch by talking to it: ignore lines, require a trigger text, keep only some lines, change the interval
- In a group the bot speaks only when mentioned or replied to; a delete needs a yes from whoever asked; a slow first check is followed up when it lands
- Every action is validated against an allow-list before it reaches changedetection; notifications are set by the app, never by the AI
- After each create or change the bot shows the text the watch sees now, so a wrong selector is visible at once
- Change notifications go from changedetection straight to the chat, in your language: what changed and a link to see it, with a reply hint to adjust the watch
- Voice notes are always answered when a transcription provider is set, in the group too
- A plugin can carry out proposals, so the bot can change how it works when someone asks; `/presentacion` tells the chat what it can do here
- English base with a Spanish translation; every string lives in a locale file
- AI provider of your choice: any OpenAI-compatible endpoint, Anthropic, or a plugin file
- The instance is the source of truth: the bot keeps no copy of the watches

## Quick start

```bash
mkdir -p data && cp docs/settings.example.yaml data/settings.yaml   # edit it: changedetection URL, locale, chat
export CD_API_KEY=... TELEGRAM_BOT_TOKEN=... SCD_DATA=data
uvx --from superchangedetection scd check
```

`scd check` prints the instance version and the watch count when the connection works; `scd run` is the bot. The Docker image, the settings and the bot setup are in [Getting started](docs/getting-started.md).

## Documentation

- [Getting started](docs/getting-started.md): prerequisites, Docker, settings, the Telegram side
- [Configuration](docs/configuration.md): every setting and its default
- [Usage](docs/usage.md): what to say to the bot, the command line
- [How it works](docs/how-it-works.md): the action block, filters, notifications, what the AI sees

## License

[GPL-3.0-or-later](LICENSE)
