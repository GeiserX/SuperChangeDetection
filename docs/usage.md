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

When a page changes, changedetection writes to the chat on its own, with the diff, a link to the change
and the watch's reference. Reply to that message to adjust the watch: the bot knows which watch it is.

Messages sent while the bot is thinking are kept and go with the next turn; a restart loses none of them.

| command | does |
|---|---|
| `/lista` (`/list`) | the watches of this chat, with the last change and any error |
| `/nuevo` (`/new`) | start a fresh conversation with the AI; the watches stay |
| `/ayuda` (`/help`, `/start`) | what the bot does and how to reach it |
| `/chatid` | this chat's id, for `settings.yaml` |

## From the command line

```bash
scd run                         # the bot (the Docker image's default command)
scd check                       # settings and connection
scd watches [--chat ID]         # the watches, numbered, as the AI sees them
scd rules                       # the AI's instructions
scd apply actions.yaml --chat ID   # run an actions block (see how-it-works.md); the watch notifies chat ID
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
