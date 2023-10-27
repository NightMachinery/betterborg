from telethon import events
import traceback
from uniborg.util import embed2, admins


@borg.on(events.NewMessage())
async def _(event):
    m = event.message
    for admin in admins:
        try:
            await borg.forward_messages(admin, m)
            print(f"forwarded {m} to {admin}")
        except:
            exc = "Julia encountered an exception. :(\n" + traceback.format_exc()
            print(exc)
            pass
