# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.

import asyncio
import traceback

from uniborg import tg_compat, util
from uniborg.constants import BOT_META_INFO_PREFIX
from telethon import events

DELETE_TIMEOUT = 2


@borg.on(events.NewMessage(pattern=r"^\.load (?P<shortname>\w+)$"))
async def load_reload(event):
    if not (await util.isAdmin(event) and event.message.forward == None):
        return
    # await event.delete()
    shortname = event.pattern_match["shortname"]
    await borg.reload_plugin(shortname, event.chat_id)


@borg.on(util.admin_cmd(r"^\.(?:unload|remove) (?P<shortname>\w+)$"))
async def remove(event):
    # await event.delete()
    shortname = event.pattern_match["shortname"]

    if shortname == "_core":
        msg = await event.respond(f"Not removing {shortname}")
    elif shortname in borg._plugins:
        borg.remove_plugin(shortname)
        msg = await event.respond(f"Removed plugin {shortname}")
    else:
        msg = await event.respond(f"Plugin {shortname} is not loaded")

    # await asyncio.sleep(DELETE_TIMEOUT)
    # await borg.delete_messages(msg.to_id, msg)


@borg.on(util.admin_cmd(r"^\.tgcaps$"))
async def tgcaps(event):
    #: Re-probed on every call, since BotFather toggles such as Guest Mode
    #: change the account flags without a restart.
    capabilities = await tg_compat.capabilities_of(borg, refresh=True)
    report = tg_compat.capabilities_report(
        capabilities,
        safety_stats=getattr(borg, "safety_stats", None),
    )
    await event.reply(f"{BOT_META_INFO_PREFIX}{report}", parse_mode=None)
