from telethon import TelegramClient, events
from functools import partial
from uniborg import util
from IPython import embed

@borg.on(events.NewMessage(pattern=r"(?i)^\.a (.*)$"))
async def _(event):
    #print("aget received")
    if await util.isAdmin(event) and event.message.forward == None:
        #print("aget by admin")
        # util.ix()
        # embed(using='asyncio')
        await util.run_and_upload(
            event=event,
            to_await=partial(
                util.simple_run, command=event.pattern_match.group(1).replace("‘","'").replace('“','"').replace("’","'").replace('”','"').replace('—','--')))
