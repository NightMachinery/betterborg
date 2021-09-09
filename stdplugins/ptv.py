from telethon import TelegramClient, events
from uniborg import util, config
from uniborg.util import aget_brishz
from functools import partial
from IPython import embed
from brish import z, zp, zs, bsh, Brish

@borg.on(events.NewMessage(pattern=r"(?i)^\.{2}ptv (.*)$"))
async def _(event):
    if not (await util.isAdmin(event, additional_admins=[*config.sis_ids]) and event.message.forward == None):
        return

    query = event.pattern_match.group(1)

    cmd=["ptv", query]
    await aget_brishz(event, cmd, album_mode=False)
