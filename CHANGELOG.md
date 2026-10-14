# Changelog

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
