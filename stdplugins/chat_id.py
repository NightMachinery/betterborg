from telethon import events


@borg.on(events.NewMessage(pattern=r"(?i)^\.chatID$", outgoing=True))
async def _(event):
    if event.forward:
        return
    event.reply(event.chat.id)
 
