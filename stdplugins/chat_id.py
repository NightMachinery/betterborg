from telethon import events


@borg.on(events.NewMessage(pattern=r"(?i)^\.chatID$"))
async def _(event):
    if event.forward:
        return
    # print(help(await event.chat))

    chat = await event.get_chat()
    await event.reply(str(chat.id))
