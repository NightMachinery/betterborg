"""Restart or Terminate the bot from any chat
Available Commands:
.restart
.shutdown"""

from telethon import events
import asyncio
import os
import sys
from uniborg.util import admin_cmd


@borg.on(admin_cmd(pattern=".restart"))
async def _(event):
    await event.reply("Restarted.")
    await borg.disconnect()
    os.execl(sys.executable, sys.executable, *sys.argv)
    # You probably don't need it but whatever
    quit()


@borg.on(admin_cmd(pattern=".shutdown"))
async def _(event):
    await event.edit("Turning off ...")
    await borg.disconnect()
    quit()
