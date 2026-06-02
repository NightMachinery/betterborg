# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.

from telethon import events
from uniborg import util
from pynight.common_icecream import ic

# TODO Support specifying the message content. The subsequent mentions should just reply to this message and have a ☝🏻 emoji.


#: Per-mode configuration. The mode name is captured (case-insensitively) from
#: the command and normalized to lowercase before lookup.
def _ids_header(event):
    return f"Users in chat number {event.chat_id}:\n"


MODE_CONFIG = {
    #: Invisible mention: silent pings, with a visible "@all" header.
    "all": {"limit": 10, "header": lambda event: "@all\n"},
    #: Full mention: visible @name links, no header.
    "allf": {"limit": 50, "header": lambda event: ""},
    #: IDs: human-readable dump of name/username/id.
    "allids": {"limit": 50, "header": _ids_header},
}


@borg.on(events.NewMessage(pattern=r"(?i)^(?:\.|@)(all|allf|allIDs)$"))
async def _(event):
    if event.fwd_from:
        return

    input_chat = await event.get_input_chat()
    if not (
        await util.isAdmin(event)
        or str(event.chat_id)
        in [
            #: The Order
            "3901506504",
            "-1003901506504",
        ]
    ):
        return

    if False:
        try:
            await event.delete()
        except:
            pass

    mode = event.pattern_match.group(1).lower()
    config = MODE_CONFIG[mode]
    mention_limit = config["limit"]
    current_mentions = 0
    mentions = ""

    def reset_mentions():
        nonlocal current_mentions
        nonlocal mentions
        current_mentions = 0
        mentions = config["header"](event)

    async def send_current_mentions():
        nonlocal mentions
        nonlocal event

        ic(mentions)

        await event.respond(mentions, reply_to=event.message.reply_to_msg_id)
        reset_mentions()

    def format_user(x):
        if mode == "allids":
            return f"{x.first_name} {x.last_name} ({x.username}): id={x.id}\n"
        elif mode == "allf":
            name = x.username or x.first_name or x.last_name or "NA"
            return f"[@{name}](tg://user?id={x.id})\n"
        elif mode == "all":
            return f"[⁣](tg://user?id={x.id})"
        else:
            raise ValueError(f"Unknown mode: {mode}")

    reset_mentions()
    async for x in borg.iter_participants(input_chat, 9000):
        if current_mentions < mention_limit:
            current_mentions += 1
            mentions += format_user(x)
        else:
            await send_current_mentions()
    if current_mentions > 0:
        await send_current_mentions()
