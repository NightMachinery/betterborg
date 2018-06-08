# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.

from telethon import events


@borg.on(events.NewMessage(pattern=r"\.all", outgoing=True))
async def _(event):
    if event.forward:
        return
    await event.delete()
    mention_limit = 30
    current_mentions = 0
    mentions = "@all\n"

    def reset_mentions():
        nonlocal current_mentions
        nonlocal mentions
        current_mentions = 0
        mentions = "@all\n"

    async def send_current_mentions():
        nonlocal mentions
        nonlocal event
        await event.respond(mentions)
        reset_mentions()

    reset_mentions()
    async for x in borg.iter_participants(await event.input_chat, 9000):
        if current_mentions < mention_limit:
            current_mentions += 1
            if event.raw_text == '.allIDs':
                # current_mentions = 1 #Effectively disables the chunking scheme and sends all output in a huge text. It might actually be undesirable since there is a limit on message size. So let's not use it.
                mentions += f"{x.first_name} {x.last_name} ({x.username}): id={x.id}\n"
            else:
                mentions += f"[\u2063](tg://user?id={x.id})"
                # mentions += f"[@{x.username}](tg://user?id={x.id})\n"
            # mentions += f"@{x.username} "
            # await event.respond(f"[Hey, {x.first_name}!](tg://user?id={x.id})")
        else:
            await send_current_mentions()
    if current_mentions > 0:
        await send_current_mentions()
