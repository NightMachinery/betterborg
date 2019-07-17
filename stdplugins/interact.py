from telethon import TelegramClient, events
from uniborg import util
from IPython import embed

@borg.on(events.NewMessage(pattern=r"(?i)^\.i$"))
async def _(event):
    if await util.isAdmin(event) and event.message.forward == None:
        util.ix()
        embed(using='asyncio')
