from telethon import TelegramClient, events
from uniborg import util
from IPython import embed


# 300815638 is the fairy, sagacious clownfish
@borg.on(events.NewMessage(from_users=['Arstar', 300815638], pattern=r"^.jlib\s+.*(\w{32})\W*$"))
async def _(event):
    if event.message.forward == None:
        md5 = event.pattern_match.group(1)
        await event.reply(f"Downloading {md5} ...")
        await util.aget(event, ('jlib.zsh', md5), False)
