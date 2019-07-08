from telethon import TelegramClient, events
from functools import partial
from uniborg import util
from IPython import embed

@borg.on(events.NewMessage(pattern=r"(?i)^\.aget (.*)$"))
async def _(event):
    #print("aget received")
    if await util.isAdmin(event):
        #print("aget by admin")
        # util.ix()
        # embed(using='asyncio')
        await util.run_and_upload(
            event=event,
            to_await=partial(
                util.simple_run, command=event.pattern_match.group(1).replace("‘","'").replace('“','"').replace("’","'").replace('”','"')), quiet=False)
