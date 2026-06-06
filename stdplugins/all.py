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
    "allf": {"limit": 4, "header": lambda event: ""},
    #: IDs: human-readable dump of name/username/id.
    "allids": {"limit": 50, "header": _ids_header},
}


def _should_skip_participant(user):
    return bool(getattr(user, "bot", False) or getattr(user, "is_self", False))


def _format_user(mode, user):
    if mode == "allids":
        return f"{user.first_name} {user.last_name} ({user.username}): id={user.id}\n"
    elif mode == "allf":
        name = user.username or user.first_name or user.last_name or "NA"
        return f"[@{name}](tg://user?id={user.id})\n"
    elif mode == "all":
        return f"[⁣](tg://user?id={user.id})"
    else:
        raise ValueError(f"Unknown mode: {mode}")


async def _iter_mention_messages(mode, event, participants):
    config = MODE_CONFIG[mode]
    mention_limit = config["limit"]
    current_mentions = 0
    mentions = config["header"](event)

    async for user in participants:
        if _should_skip_participant(user):
            continue

        if current_mentions >= mention_limit:
            yield mentions
            current_mentions = 0
            mentions = config["header"](event)

        current_mentions += 1
        mentions += _format_user(mode, user)

    if current_mentions > 0:
        yield mentions


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

    async for mentions in _iter_mention_messages(
        mode, event, borg.iter_participants(input_chat, 9000)
    ):
        ic(mentions)
        await event.respond(mentions, reply_to=event.message.reply_to_msg_id)
