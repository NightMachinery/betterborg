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

pattern_a = re.compile(r"(?im)^(?:\[(?:(?:In reply to)|(?:Forwarded from))\s+[^]]*\]\n)*\.a(?P<nobrish>a)?(?P<fork>f)?(?P<noalbum>d)?(?P<noglob>n)?\s+(?P<cmd>(?:.|\n)*)$")


@borg.on(events.NewMessage(pattern=pattern_a))
async def _(event):
    if not (await util.isAdmin(event) and event.message.forward == None):
        return

    match = event.pattern_match
    album_mode = not bool(match.group('noalbum'))
    brish_mode = not bool(match.group('nobrish'))
    command = await clean_cmd(match.group('cmd'))
    if match.group('noglob') == 'n':
        command = 'noglob ' + command
    fork = True
    if match.group('fork') == 'f':
        fork = False
    if brish_mode:
        to_await=partial(brishz, cmd=command, fork=fork)
    else:
        to_await=partial(util.simple_run, command=command, shell=True)
    await util.run_and_upload(event=event, to_await=to_await, album_mode=album_mode)

@borg.on(util.admin_cmd(pattern="^\.xf$"))
async def _(event):
    util.init_brishes()
    await event.reply("Reinitialized brishes. Note that old running instances can still rejoin.")


@borg.on(util.admin_cmd(pattern="^\.(x|sbb)$"))
async def _(event):
    util.restart_brishes()
    await event.reply("Restarted brishes.")
