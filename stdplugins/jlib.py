from telethon import TelegramClient, events
from uniborg import util
from IPython import embed


# 300815638 is the fairy, sagacious clownfish
clownfish = 300815638


@borg.on(events.NewMessage(from_users=['Arstar', clownfish], pattern=r"^.jlib\s+.*(\w{32})\W*$"))
async def _(event):
    if event.message.forward == None:
        md5 = event.pattern_match.group(1)
        await event.reply(f"Downloading {md5} ...")

        ##
        # allowing automatic send to kindle
        # msg = getattr(event, 'message', None)
        # sender = getattr(msg, 'sender', getattr(event, 'sender', None))
        # sid = sender.id
        # kemail = ''
        # semail = ''
        # if sid == clownfish:
        #     kemail = ''
        #     semail = ''
        ##

        await util.aget(event, ('jlib.zsh', md5), False)
