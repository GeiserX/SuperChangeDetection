# Usage

## In the chat

Write what you want in your own words, with the link:

- «Avísame cuando cambie el precio aquí: https://tienda.example/teclado»
- «Vigila si sale una versión nueva en https://example.com/releases, mira una vez al día»
- «El 3 avisa demasiado, solo me interesa cuando cambie el precio»
- «Borra el de los conciertos»

In a group the bot answers a message that mentions its `@handle` or replies to one of its messages; in a
private chat it always answers. After a create or a change it shows the text the watch sees now and reminds
you to tell it when something is off. A delete is offered with two buttons and runs only after the person
who asked taps Sí (or types sí or no in reply), within the hour. When a first check has not finished by the
time the bot answers, it looks again every half minute and posts what the watch sees once the check ran.

When a page changes, changedetection writes to the chat on its own: the watch, its URL, a link to the
change on the instance and the watch's reference. No diff, no AI: click the link and read the change. Reply
to that message to adjust the watch: the bot knows which watch it is.

With a transcription provider, a voice note is always answered, in the group too: the bot writes what it
heard (`🎤 …`) and then handles it as if you had typed it.

With a plugin provider you can send a photo, a screenshot or a PDF too, in the group as well, with what you
want in its caption: «vigila el precio de esto».

With a plugin that can carry out proposals, you can also ask the bot to work differently («añade un
comando que…», «avisa de otra forma…»): it says what it will do and does it outside the chat, and tells you
how it went. Only the people in `bot.users` can ask for that.

When there is a choice to make the bot puts buttons under its answer: send it only a link and it offers
«Cada hora», «Cada día», «Solo una vez» or «No». Tap one, or just keep talking. When it could do
something useful it offers it with buttons and the line «Si no respondes, no hago nada.»; the buttons
expire and nothing happens. Ask it to remind you («recuérdame mirar esta página el viernes») and it offers
a reminder here with a button; at that time it writes in the chat, mentioning you.

Several messages sent together get one answer, and so do messages sent while the bot is thinking: it
takes them into the answer it is writing. Edit a message and the bot goes by the new wording, even after it
answered: it says what changes. A restart loses no message.

| command | does |
|---|---|
| `/lista` (`/list`) | the watches of this chat, with the last change and any error |
| `/nuevo` (`/new`) | start a fresh conversation with the AI; the watches stay |
| `/ayuda` (`/help`, `/start`) | what the bot does and how to reach it |
| `/presentacion` (`/intro`) | the bot introduces itself: how to ask, how to fix a watch, what else it can do here |
| `/recordatorios` (`/reminders`) | the reminders still to come in this chat, each with a button to cancel it |
| `/chatid` | this chat's id, for `settings.yaml` |

## From the command line

```bash
scd run                         # the bot (the Docker image's default command)
scd check                       # settings and connection
scd watches [--chat ID]         # the watches, numbered, as the AI sees them
scd rules                       # the AI's instructions
scd apply actions.yaml --chat ID   # run an actions block (see how-it-works.md); the watch notifies chat ID
scd intro [--chat ID]           # post the bot's introduction (the /presentacion text) to the chat
```

An actions file is the list from the block, with or without the fence:

```yaml
- op: create
  url: https://example.com/
  title: Example
  include_filters: ["h1"]
  time_between_check: {hours: 12}
```

`scd apply` exits non-zero when any action failed or its first check reported an error.
