from telethon import TelegramClient, events
from uniborg import util
from IPython import embed

@borg.on(events.NewMessage(pattern=r"(?i)^\.a (.*)$"))
async def _(event):
    #print("aget received")
    if await util.isAdmin(event) and event.message.forward == None:
        await util.aget(event)

# @borg.on(events.NewMessage(pattern=r"(?i)^\.ac (.*)$", outgoing=True))
# async def _(event):
#     if event.message.forward == None:
#         await util.aget(event)

