# How it works

## The pieces

- **changedetection.io** fetches pages on a schedule, keeps the text that passes each watch's filters, and
  notifies when it changes. It is the source of truth: the bot stores no watch.
- **The app** (`superchangedetection`) talks to the instance's API v1 with the API key, builds what the AI
  sees, validates and applies the AI's actions, and renders the chat messages from the locale file.
- **The AI** reads the page and the request, writes a short answer and, when something must change, one
  `actions` block. It never touches the instance itself.

## One turn

1. A message arrives (in the chat, or an actions file on the command line). In the chat the line the AI
   gets is `Name: text`; a reply to a change notification adds which watch it is about, read from the
   `ref <uuid>` line of the notification.
2. The app builds the AI's workspace: `CLAUDE.md`-style rules (`scd rules`), `context.md` with every watch
   numbered (title, url, filters, interval, backend, last check, last change, last error, which chat it
   notifies), `pages/<n>.html` for every URL in the new messages (scripts, styles and long attributes
   removed, capped), `snapshots/<uuid>.txt` (each watch's latest text) and `diffs/<uuid>.txt` (its last change).
3. The AI answers. A block like this may end the answer:

   ```actions
   - op: create
     url: https://example.com/product
     title: Precio del teclado
     include_filters: [".product-price"]
     time_between_check: {hours: 6}
   - op: update
     watch: 3
     set: {ignore_text: ["Visitas:"], trigger_text: ["En stock"]}
   - op: delete
     watch: 2
   - op: recheck
     watch: 1
   ```

4. The app validates the block: known ops, a watch number or uuid that exists, fields from the allow-list in
   `actions.ALLOWED` with the right types, an interval over zero, known backends and processors. Anything
   else is refused with the reason.
5. The app applies it: a create gets the instance's defaults (interval, backend, tag) plus the
   notification fields; an update on a watch the bot does not own repoints its notification to the asking
   chat; every create, update and recheck triggers a check and waits up to `first_check_wait_s`.
6. The chat gets the result: what the watch sees now (the latest snapshot, trimmed), the check's error if
   any, or a note that the check has not finished, then the standing reminder to say when a watch is wrong.
   A delete is not run in this step: the chat gets the question with Sí and No, and only the person who
   asked can answer it, within the hour. A check that had not finished is looked at again every half
   minute for up to 30 minutes, and the chat is told what the watch sees once it ran.
7. The next turn tells the AI what the app did with its block (the results as shown, a refused block
   and why, a delete confirmed or declined), so it never has to guess.

## The conversation

Each chat is one conversation. A plugin provider keeps it on its side and gets only the new lines each
turn, plus a recap of the recent ones (`bot.history` lines) for when it has to start over; the openai
and anthropic providers get the recap every time (with `bot.history_days`, every line of those days too).
`/nuevo` starts over. Lines wait in SQLite under the data directory until a turn the provider kept has
taken them, so a restart loses none; a turn the provider did not keep (`kept: false`) sends them again.

One answer per burst. A chat's turn starts 2.5 seconds after its newest message, and waits up to 30
seconds for a voice note of that chat still being transcribed, so messages sent together go as one turn.
A message that calls the bot while a turn runs is handed to that turn (`turn["incoming"]`, for a plugin
provider); the answer names the ones it took in, and only the rest make the next turn. One turn runs at a
time per chat. An answer to one message replies to it; an answer to several is sent plainly.

Edits count. Telegram sends the bot edited messages, and each is handled by where the message is: still
waiting, its new text goes; in the running turn, a correction with the old and the new wording joins
it; already answered, the correction and the answer it got start a new turn; a group message that did
not call the bot and now mentions it is answered. An edit that changes only spacing does nothing, one
that starts with `/` never runs a command, and an edit is never a yes or a no. The log keeps the final
wording.

With `bot.history_scope: all`, the group's messages that do not call the bot are kept too and go to the
AI with its next turn, marked `chat:`, as context it does not answer. In a group, Telegram shows a bot
those messages and their edits only when it is an admin of the group (or privacy mode is off).

## Notifications

Every watch the bot creates carries one apprise URL, `tgram://<bot token>/<chat id>/`, so changedetection
sends the change to the chat as the bot, with no code of ours in the path. The title and body are the
`notification_title` and `notification_body` strings of the locale file, in changedetection's Jinja
syntax: the watch's title and URL, a link to the change on the instance (`{{diff_url}}`, which needs
`BASE_URL` set on changedetection), the reminder to reply when the watch is wrong, and a last line
`ref <uuid>` so a reply to that message can be tied to the watch. No diff: the person clicks the link and
reads the change on the instance. At start the bot puts the current template on every watch it owns, so
a change to those strings reaches the old watches too. That URL is also how the app knows which watches
are its own and which chat each one belongs to.

## Voice notes

With a `transcription` provider, a voice note or audio file from someone allowed is always handled, in
the group too, since a note cannot mention the bot or reply by itself: the audio is fetched from
Telegram, transcribed, echoed as a reply (`🎤 …`) so everyone sees what was heard, and then treated as
typed text, followed by its caption if it has one. A note that
could not be transcribed gets a short reply saying so.

## Proposals

A plugin provider that defines `act()` may answer with proposals: things it would do outside the chat,
such as changing the bot's own code. The app shows each summary; with `confirm_by: none` it runs `act()`
at once in the background and posts what came of it, with `asker` after a yes from the person who
asked, with `admins` after a yes from one of `bot.users`. Only `bot.users` may ask for a proposal. A
proposal that was running when the bot stopped is reported as interrupted at the next start. The chat
never sees code: what the plugin says about a change is its own text.

## What the AI may and may not do

May: create, update, delete (confirmed by the asker) and recheck watches; set the fields in the allow-list
(url, title, selectors, ignore and trigger texts, extraction, interval, backend, processor, paused, muted,
headers, method, body, tags). May not: set where a notification goes or what it says, touch instance
settings, or reach anything but the files the app gives it. A deployment that wants it stricter edits the
allow-list.
