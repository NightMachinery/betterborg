from telethon import TelegramClient, events
from functools import partial
from uniborg import util
import shlex


@borg.on(events.NewMessage(pattern=r"(?i)^\.a2trr (.*)$"))
async def _(event):
    # if await util.isAdmin(event):
    await util.run_and_upload(
        event=event,
        to_await=partial(
            util.safe_run,
            command=
            'aria2c --seed-time=0  --ca-certificate /etc/ssl/certs/ca-certificates.crt '
            + shlex.quote(event.pattern_match.group(1)))
        # + '"')
        ,
        quiet=False)
