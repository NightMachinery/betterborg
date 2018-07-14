from telethon import TelegramClient, events
from functools import partial
from uniborg import util


@borg.on(events.NewMessage(pattern=r"(?i)^\.aget (.*)$"))
async def _(event):
    # util.interact(locals())
    util.borg = borg
    # util.interact(locals())
    if await util.isAdmin(event):
        # util.interact(locals())
        await util.run_and_upload(
            event=event,
            to_await=partial(
                util.simple_run, command=event.pattern_match.group(1)),
            quiet=False)
