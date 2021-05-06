from telethon import events
from uniborg import util
from uniborg.util import admin_cmd, embed2
from brish import z


@borg.on(events.NewMessage(pattern=r"(?i)^\.del (?P<self_only>s?)\s*(?P<n>\d+)$"))
async def _(event):
    # USERBOT ONLY (Can't get_messages in bot API)

    # embed2()
    if not (await util.isAdmin(event) and event.message.forward == None):
        return
    await event.delete()
    n = int(event.pattern_match.group('n'))
    self_only = bool(event.pattern_match.group('self_only'))
    print(f"del received: {n}")
    chat = await event.get_chat()
    for msg in await borg.get_messages(chat, limit=n):
        if self_only and not await util.isAdmin(None, msg=msg):
            continue

        await msg.delete()
        # embed2()
