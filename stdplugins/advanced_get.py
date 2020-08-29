from telethon import TelegramClient, events, Button
import itertools
import os
from pathlib import Path
import uuid
import subprocess
from uniborg import util
from uniborg.util import clean_cmd, embed2, brishz
from IPython import embed
import re
import asyncio
from brish import z, zp, Brish
from functools import partial

pattern_a = re.compile(r"(?im)^\.a(n?)(f?)\s+((?:.|\n)*)$")
pattern_aa = re.compile(r"(?im)^\.aa(n?)\s+((?:.|\n)*)$")


@borg.on(events.NewMessage(pattern=pattern_a))
async def _(event):
    if not (await util.isAdmin(event) and event.message.forward == None):
        return

    match = event.pattern_match
    command = await clean_cmd(match.group(3))
    if match.group(1) == 'n':
        command = 'noglob ' + command
    fork = True
    if match.group(2) == 'f':
        fork = False

    await util.run_and_upload(event=event, to_await=partial(brishz, cmd=command, fork=fork))


@borg.on(events.NewMessage(pattern=pattern_aa))
async def _(event):
    #print("aget received")
    if await util.isAdmin(event) and event.message.forward == None:
        await util.aget(event)


@borg.on(util.admin_cmd(pattern=".xf"))
async def _(event):
    util.init_brishes()
    await event.reply("Reinitialized brishes. Note that old running instances can still rejoin.")


@borg.on(util.admin_cmd(pattern=".(x|sbb)"))
async def _(event):
    util.restart_brishes()
    await event.reply("Restarted brishes.")
