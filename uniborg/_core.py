# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.

import asyncio
import traceback

from uniborg import util
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
