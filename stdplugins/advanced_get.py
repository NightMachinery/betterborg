from telethon import TelegramClient, events
from uniborg import util
from IPython import embed
import re

p = re.compile(r"(?im)^\.a(n?) ((?:.|\n)*) fin$")
    
@borg.on(events.NewMessage(pattern=r"(?im)^\.a(n?)\s+((?:.|\n)*)$"))
async def _(event):
    #print("aget received")
    if await util.isAdmin(event) and event.message.forward == None:
        await util.aget(event)

@borg.on(events.InlineQuery)
async def handler(event):
    query = event.text.lower()
    m = p.match(query)
    if (not await util.isAdmin(event)) or m == None:
        # util.ix()
        # embed(using='asyncio')
        return
    print(query)
    builder = event.builder
    result = builder.article('aget', text=m.group(2), link_preview=False)

    # NOTE: You should always answer, but we want plugins to be able to answer
    #       too (and we can only answer once), so we don't always answer here.
    if result:
        await event.answer([result]) #returns true
        # util.ix()
        # embed(using='asyncio')

