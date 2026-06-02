###
# * @usage
# ** `.del s 99999999`
#
# * @warning =self_only= is currently implemented as admin-only instead!
###
from telethon import events
from telethon.tl.functions.messages import (
    GetMessagesReactionsRequest,
    SendReactionRequest,
)
from telethon.tl.types import UpdateMessageReactions
from uniborg import util
from uniborg.util import admin_cmd, embed2
from brish import z
from icecream import ic
from tqdm.asyncio import tqdm


REACTION_REFRESH_CHUNK_SIZE = 100


def _reactions_have_own_reaction(reactions):
    if not reactions:
        return False

    for result in getattr(reactions, "results", None) or []:
        if getattr(result, "chosen_order", None) is not None:
            return True

    for reaction in getattr(reactions, "recent_reactions", None) or []:
        if getattr(reaction, "my", False):
            return True

    return False


def _has_own_reaction(msg):
    return _reactions_have_own_reaction(getattr(msg, "reactions", None))


def _has_min_reactions(msg):
    return bool(getattr(getattr(msg, "reactions", None), "min", False))


def _iter_reaction_updates(updates):
    if isinstance(updates, UpdateMessageReactions):
        yield updates
        return

    update = getattr(updates, "update", None)
    if isinstance(update, UpdateMessageReactions):
        yield update

    for update in getattr(updates, "updates", None) or []:
        if isinstance(update, UpdateMessageReactions):
            yield update


def _chunks(items, size):
    for index in range(0, len(items), size):
        yield items[index : index + size]


async def _clear_own_reaction(chat, msg):
    await borg(SendReactionRequest(peer=chat, msg_id=msg.id, reaction=[]))


async def _clear_confirmed_min_reactions(chat, min_reaction_messages):
    if not min_reaction_messages:
        return 0

    reaction_delete_count = 0
    min_reaction_ids = list(min_reaction_messages.keys())
    for message_ids in _chunks(min_reaction_ids, REACTION_REFRESH_CHUNK_SIZE):
        try:
            updates = await borg(
                GetMessagesReactionsRequest(
                    peer=chat,
                    id=message_ids,
                )
            )
        except Exception as e:
            print(f"failed to fetch reduced reactions: {e}", flush=True)
            continue

        for update in _iter_reaction_updates(updates):
            if not _reactions_have_own_reaction(getattr(update, "reactions", None)):
                continue

            msg = min_reaction_messages.get(update.msg_id)
            if not msg:
                continue

            try:
                await _clear_own_reaction(chat, msg)
                reaction_delete_count += 1
            except Exception as e:
                print(f"failed to delete reaction on message {msg.id}: {e}", flush=True)

    return reaction_delete_count


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
    reaction_delete_count = 0
    min_reaction_messages = {}

    async for msg in tqdm(
        borg.iter_messages(chat, limit=n), total=n, desc="Deleting messages"
    ):
        delete_msg = not self_only or await util.isAdmin(None, msg=msg)

        if self_only:
            if _has_own_reaction(msg):
                try:
                    await _clear_own_reaction(chat, msg)
                    reaction_delete_count += 1
                except Exception as e:
                    print(f"failed to delete reaction on message {msg.id}: {e}", flush=True)
            elif not delete_msg and _has_min_reactions(msg):
                min_reaction_messages[msg.id] = msg

        if not delete_msg:
            continue

        ic(msg.raw_text)
        await msg.delete()
        delete_count += 1
        # embed2()

    print(f"deleted {delete_count} messages!", flush=True)
    if self_only:
        reaction_delete_count += await _clear_confirmed_min_reactions(
            chat, min_reaction_messages
        )
        print(f"deleted {reaction_delete_count} reactions!", flush=True)
