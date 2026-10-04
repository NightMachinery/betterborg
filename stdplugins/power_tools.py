"""Restart or Terminate the bot from any chat
Available Commands:
.restart
.shutdown"""

from telethon import events
import asyncio
import os
import sys
from uniborg import shell_stream
from uniborg.util import admin_cmd

#: The tasks that outlive their handler, kept so they are not collected.
_TASKS = set()


def _restart():
    os.execl(sys.executable, sys.executable, *sys.argv)


def _quit():
    sys.exit()


async def _disconnect_then(then):
    await shell_stream.stop_all_and_disconnect(borg)
    then()


def _after_the_handler(then):
    """Stops the running shell commands and disconnects, then calls THEN, in
    a task of its own.

    The commands' finals go out before the disconnect. `disconnect` cancels
    every running event handler, the one that calls it included, so nothing
    after it would run in the handler.
    """
    task = asyncio.ensure_future(_disconnect_then(then))
    _TASKS.add(task)
    task.add_done_callback(_TASKS.discard)


@borg.on(admin_cmd(pattern=".restart"))
async def restart_handler(event):
    await event.reply("Restarted.")
    _after_the_handler(_restart)


@borg.on(admin_cmd(pattern=".shutdown"))
async def shutdown_handler(event):
    #: A reply, not an edit: a bot cannot edit the admin's message.
    await event.reply("Turning off ...")
    _after_the_handler(_quit)
