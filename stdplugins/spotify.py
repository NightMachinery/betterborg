from telethon import TelegramClient, events
from uniborg import util
from IPython import embed

@borg.on(events.NewMessage(pattern=r"(?i)^\.s (.*)$"))
async def _(event):
    if await util.isAdmin(event) and event.message.forward == None:
        await util.aget(event, ('spotdl', '-f', '.', '-s', event.pattern_match.group(1)), False)

