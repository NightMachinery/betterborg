from uniborg.util import embed2
import uniborg.util as util
from telethon import events
from telethon.tl.functions.contacts import GetContactsRequest

# import json
from brish import z, zp
from IPython import embed


@borg.on(events.NewMessage(pattern=r"(?i)^\.contacts$"))
async def _(event):
    if not (await util.isAdmin(event) and event.message.forward == None):
        return

    result = await borg(GetContactsRequest(hash=0))
    await event.reply(
        f"saved_count: {result.saved_count}\ncontacts: {len(result.contacts)}\nusers: {len(result.users)}"
    )

    async def give_contacts(cwd, **kwargs):
        c = z("jq > {cwd}/contacts.json", cmd_stdin=result.to_json())

    #
    # embed2()
    await util.run_and_upload(event=event, to_await=give_contacts)
