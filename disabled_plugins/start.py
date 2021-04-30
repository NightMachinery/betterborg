from telethon import events
from uniborg.util import admin_cmd, embed2, aget
from brish import z


@borg.on(admin_cmd(pattern=r"(?i)^\/start (.*)$"))
async def _(event):
    # Doesn't trigger with whitespace. So we use base64. Note that url-encoding is useless.
    # Even with base64 there is a max len limit!!
    # TODO So we should implement a redis-based ID-to-cmd. We can use the first char to set interpretation mode, since just the magnet hash is less than max length. This way we will have the best of both worlds.

    # embed2()
    # event.edit(f".a {event.pattern_match.group(1)}")
    cmd = event.pattern_match.group(1)
    cmdd = z("<<<{cmd} base64 -D").outrs
    print(f"start received: {cmdd}")
    await aget(event, command=cmdd)
