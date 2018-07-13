from telethon import events
from uniborg import util


@borg.on(util.admin_cmd(pattern=r"(?i)^\.aget (.*)$"))
async def _(event):
    await borg.reply("lo")
    # if await util.isAdmin(event):
        # await event.delete()
    
