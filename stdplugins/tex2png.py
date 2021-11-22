from telethon import TelegramClient, events
from uniborg import util
from functools import partial
from uniborg.util import embed2, brishz
from brish import zs

@borg.on(events.NewMessage(pattern=r"^\.tex\s+(.+)$"))
async def _(event):
    if event.message.forward == None and event.sender:
        tex = event.pattern_match.group(1)
        if not tex:
            return

        print(
            f"tex2png (user_id={event.sender.id}, username={event.sender.username}): {tex}"
        )
        # await event.reply(f"Processing {tex} ...")

        command = zs("tex2png {tex}")
        await util.run_and_upload(
            event=event, to_await=partial(brishz, cmd=command, fork=True, shell=False)
        )
