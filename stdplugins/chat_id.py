from telethon import events
from uniborg.util import embed2


@borg.on(events.NewMessage(pattern=r"(?i)^\.chatID$"))
async def _(event):
    if event.forward:
        return
    # print(help(await event.chat))

    chat = await event.get_chat()
    # await event.reply(str(chat.id))
    await event.reply(str(chat.__dict__))

    r_id = event.message.reply_to_msg_id
    if r_id:
        m2 = await borg.get_messages(chat, ids=r_id)
        # embed2()
        await event.reply(f"{m2.__dict__}")
        # await event.reply(f"{m2.document.__dict__}")
        if m2.forward:
            await event.reply(f"{m2.forward.__dict__}")
        if m2.file:
            await event.reply(f"{m2.file.id}")
        # if m2.media:
        #     await event.reply(f"{m2.media.__dict__}")
