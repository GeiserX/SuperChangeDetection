# Usage

## In the chat

Write what you want in your own words, with the link:

- «Avísame cuando cambie el precio aquí: https://tienda.example/teclado»
- «Vigila si sale una versión nueva en https://example.com/releases, mira una vez al día»
- «El 3 avisa demasiado, solo me interesa cuando cambie el precio»
- «Borra el de los conciertos»

In a group the bot answers a message that mentions its `@handle` or replies to one of its messages; in a
private chat it always answers. After a create or a change it shows the text the watch sees now and reminds
you to tell it when something is off. A delete is confirmed by the person who asked before it happens.

When a page changes, changedetection writes to the chat on its own, with the diff, a link to the change
and the watch's reference. Reply to that message to adjust the watch.

## From the command line

```bash
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
