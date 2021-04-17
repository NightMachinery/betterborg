from telethon import TelegramClient, events
from uniborg import util
from IPython import embed
from functools import partial
from uniborg.util import embed2, brishz
from brish import zs


@borg.on(events.NewMessage(pattern=r"^http.*(\w{32})\W*$"))
async def _(event):
    if event.message.forward == None and event.sender:
        md5 = event.pattern_match.group(1)
        print(
            f"User (id={event.sender.id}, username={event.sender.username}) requested {md5}"
        )
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

        command = zs("lgNoBok=y pkno sout jlib {md5}")
        await util.run_and_upload(
            event=event, to_await=partial(brishz, cmd=command, fork=True, shell=False)
        )
