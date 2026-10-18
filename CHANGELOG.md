# Changelog

## 0.6.0

- When the AI has no capacity left, the bot waits for it instead of failing. A plugin raises an error
  with a `retry_after` attribute (seconds); the chat is told once how long the wait is, the messages
  stay queued (a restart keeps them), and the bot answers them by itself when the wait is over, with
  what was sent meanwhile in the same answer. Until now the chat heard "the assistant is not
  answering" and nothing happened until someone wrote again.
- A merge to main that changes `__version__` publishes that version's image and tags the commit, so a
  release no longer needs a tag pushed by hand. A tag pushed by hand still publishes.

## 0.5.2

- A plugin provider now knows which chat a turn is for and who asked: `turn["chat"]` is the chat's
  Telegram id and `turn["asker"]` the user id of the person whose line started the turn (the oldest
  line that calls the bot). A plugin can use them to reach that chat's own history elsewhere, such as
  an earlier photo someone replies to. The built-in providers ignore them.
- A reply to someone else's message says so on its line: `Ana (replying to Luis's photo of
  2026-10-14T17:46:40+00:00): did you see it?`. Until now the AI read only `Ana: did you see it?` and had
  no way to tell which photo was meant.

## 0.5.1

- Any file reaches the AI, not only photos, PDFs and images: a Word or Excel file, a CSV, an archive, keeping
  its own name and extension (one with no mime type is `application/octet-stream`). Videos, GIFs, video
  notes and stickers go the same way (`video.mp4`, `animation.mp4`, `video-note.mp4`,
  `sticker.webp`/`.webm`/`.tgs`), and so do voice notes and audio files when there is no transcription
  provider. The same caps apply. Whether the AI can open a file is the provider's to say; the "I only read
  photos and PDFs" reply is gone.

## 0.5.0

- The bot sees photos and files. A photo, or a PDF, JPEG, PNG or WebP sent as a file, is answered like a
  voice note, in the group too, with its caption as the message. The turn downloads it (20 MB each, 25 MB
  a turn) and a plugin provider gets it as `turn["attachments"]`, named `inbox/<message id>-<name>`.
  Until now a photo was dropped without a word, and only its caption reached the AI. Another kind of
  file, one too big and one that cannot be downloaded get a short reply.

## 0.4.4

- The shared principles now treat the household group as a place of trust: in it, anyone may ask about
  anyone else's data and the AI answers, unless the household turned that off in its settings. A
  private chat gets that person's own data, and an admin may ask about anyone. Nothing private goes
  public, and the AI never asks one member to get another's permission in the group.

## 0.4.3

- The AI reads the shared assistant principles first, in the chat's language: whom it answers, buttons,
  offers, privacy and honest results. The app ships them in `superchangedetection/principles/`, so an
  instance needs no plugin for them. The app's own rules no longer repeat what the principles say,
  so the 0.4.2 paragraph on «Sí», «No» and «Nada» now comes from the principles.

## 0.4.2

- The AI's rules now say a tap is an action: a yes or no offer gets «Sí» and «No», the button that
  declines among several reads «No», and «Nada» never appears on a button. «nada» stays only in
  «Si no respondes, no hago nada.».

## 0.4.1

- The button that declines says «No», not «Nada»: a tap is an action. That covers the «Cada hora» /
  «Cada día» / «Solo una vez» choice and the reminder offer. «Sin respuesta: nada» still marks an offer
  nobody tapped. Offers already posted keep working.

## 0.4.0

- Buttons under an answer when there is a choice to make. Send a link alone and the bot offers how
  often to look; a yes or no question gets Sí and No. You can always just keep talking instead.
- The bot offers what it could do next with buttons and the line «Si no respondes, no hago nada.».
  The buttons expire, and an expired offer does nothing.
- Reminders. Ask to be reminded and the bot offers one here with a button. At that time it writes in the
  chat, mentioning you, and can check a watch again. `/recordatorios` lists them, each with a button to
  cancel it.
- The AI's rules now say how people reach it. A remark between people about the bot is never an order.
- A delete is asked as «¿Dejo de vigilar …?», with a line saying you can reply with something else.

## 0.3.0

- A burst of messages gets one answer, a message sent while the bot thinks joins that answer, and edited
  messages are read. Every voice note or audio file is transcribed.

## 0.2.1

- The command menu shows in a Telegram client in any language.

## 0.2.0

- Link-only change alerts, voice notes always answered, and changes to the bot asked for in the chat.

## 0.1.0

- First release: say in the chat what to watch, the AI sets up the watch and the chat hears when it
  changes. A delete is asked first.
