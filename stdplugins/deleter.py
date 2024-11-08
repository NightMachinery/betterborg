###
# * @usage
# ** `.del s 99999999`
#
# * @warning =self_only= is currently implemented as admin-only instead!
###
from telethon import events
from uniborg import util
from uniborg.util import admin_cmd, embed2
from brish import z
from icecream import ic
from tqdm.asyncio import tqdm


@borg.on(events.NewMessage(pattern=r"(?i)^\.del\s+(?P<self_only>s?)\s*(?P<n>\d+)$"))
async def _(event):
    # USERBOT ONLY (Can't get_messages in bot API)

    # embed2()
    if not (await util.isAdmin(event) and event.message.forward == None):
        # print("deleter: not admin")
        return

    await event.delete()

    n = int(event.pattern_match.group("n") or 1)
    self_only = bool(event.pattern_match.group("self_only"))
    print(f"del received: n={n}, self_only={self_only}", flush=True)

    chat = await event.get_chat()
    delete_count = 0

    async for msg in tqdm(borg.iter_messages(chat, limit=n), total=n, desc="Deleting messages"):
        if self_only and not await util.isAdmin(None, msg=msg):
            # print(f"skipped non-self msg: {msg}")
            continue

        ic(msg.raw_text)
        await msg.delete()
        delete_count += 1
        # embed2()

    print(f"deleted {delete_count} messages!", flush=True)
