from telethon import events


@borg.on(events.NewMessage(pattern=r"(?i)^\.chatID$"))
async def _(event):
    if event.forward:
        return
    # print(help(await event.chat))
    await event.reply(str((event.chat).id))
 
