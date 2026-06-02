###
# * @usage
# ** `.del s 99999999`
#
# * @warning =self_only= is currently implemented as admin-only instead!
###
from telethon import events
from telethon.tl.functions.messages import SendReactionRequest
from uniborg import util
from uniborg.util import admin_cmd, embed2
from brish import z
from icecream import ic
from tqdm.asyncio import tqdm


def _has_own_reaction(msg):
    reactions = getattr(msg, "reactions", None)
    if not reactions:
        return False

    if getattr(reactions, "min", False):
        return True

    for result in getattr(reactions, "results", None) or []:
        if getattr(result, "chosen_order", None) is not None:
            return True

    for reaction in getattr(reactions, "recent_reactions", None) or []:
        if getattr(reaction, "my", False):
            return True

    return False


async def _clear_own_reaction(chat, msg):
    await borg(SendReactionRequest(peer=chat, msg_id=msg.id, reaction=[]))


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

    async for msg in tqdm(
        borg.iter_messages(chat, limit=n), total=n, desc="Deleting messages"
    ):
        if self_only and _has_own_reaction(msg):
            try:
                await _clear_own_reaction(chat, msg)
                reaction_delete_count += 1
            except Exception as e:
                print(f"failed to delete reaction on message {msg.id}: {e}", flush=True)

        delete_msg = not self_only or await util.isAdmin(None, msg=msg)
        if not delete_msg:
            continue

        ic(msg.raw_text)
        await msg.delete()
        delete_count += 1
        # embed2()

    print(f"deleted {delete_count} messages!", flush=True)
    if self_only:
        print(f"deleted {reaction_delete_count} reactions!", flush=True)
