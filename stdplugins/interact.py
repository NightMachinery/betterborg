from telethon import TelegramClient, events
from uniborg import util
from IPython import embed


@borg.on(events.NewMessage(pattern=r"(?i)^\.i$"))
async def _(event):
    # is somewhat buggy (hangs on exit for me), but works
    # you might need hacks like `a=borg` to await borg

    if await util.isAdmin(event) and event.message.forward == None:
        ##
        # some_user = await borg.get_entity('Username')
        # you can get, e.g., their default name and id using this
        ##
        util.ix()
        embed(using='asyncio')
