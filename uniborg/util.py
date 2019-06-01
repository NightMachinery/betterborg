# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.

from aioify import aioify
import uuid
import traceback
import os
import pexpect
import shlex
import re
from uniborg import util
from telethon import TelegramClient, events
from telethon.tl.functions.messages import GetPeerDialogsRequest
from IPython import embed

dl_base = 'dls/'
pexpect_ai = aioify(pexpect)
os_aio = aioify(os)
borg = None


def admin_cmd(pattern):
    return events.NewMessage(outgoing=True, pattern=re.compile(pattern))
    # return events.NewMessage(chats=admins, pattern=re.compile(pattern))


def interact(local=None):
    if local is None:
        local = locals()
    import code
    code.interact(local=local)

def ii():
    import nest_asyncio
    nest_asyncio.apply()

async def isAdmin(event,
                  admins=("Orphicality", ),
                  adminChats=("whitegloved", )):
    chat = await event.get_chat()
    await event.message.get_sender()
    #ii()
    #embed(using='asyncio')
    return (event.message.sender is not None and
            ((event.message.sender).is_self or (event.message.sender).username in admins)) or (
                chat.username is not None and chat.username in adminChats)


async def is_read(borg, entity, message, is_out=None):
    """
    Returns True if the given message (or id) has been read
    if a id is given, is_out needs to be a bool
    """
    is_out = getattr(message, "out", is_out)
    if not isinstance(is_out, bool):
        raise ValueError(
            "Message was id but is_out not provided or not a bool")
    message_id = getattr(message, "id", message)
    if not isinstance(message_id, int):
        raise ValueError("Failed to extract id from message")

    dialog = (await borg(GetPeerDialogsRequest([entity]))).dialogs[0]
    max_id = dialog.read_outbox_max_id if is_out else dialog.read_inbox_max_id
    return message_id <= max_id


async def run_and_get(event, to_await, cwd=None):
    if cwd is None:
        cwd = dl_base + str(uuid.uuid4()) + '/'
    await pexpect_ai.run('bash -c "mkdir -p ' + cwd + '"')
    # util.interact(locals())
    await to_await(cwd=cwd, event=event)
    # util.interact(locals())
    return cwd + str(
        await pexpect_ai.run('bash -c "ls -p | grep -E -v \'/|\.aria2.*|\.torrent$\'"', cwd=cwd),
        'utf-8').strip()


async def run_and_upload(event, to_await, quiet=True):
    file_add = ''
    # util.interact(locals())
    try:
        trying_to_dl = await util.discreet_send(
            event, "Julia is processing your request ...", event.message,
            quiet)
        file_add = await run_and_get(event=event, to_await=to_await)
        # util.interact(locals())
        base_name = str(await os_aio.path.basename(file_add))
        trying_to_upload_msg = await util.discreet_send(
            event, "Julia is trying to upload \"" + base_name +
            "\".\nPlease wait ...", trying_to_dl, quiet)
        sent_file = await borg.send_file(
            await event.get_chat(),
            file_add,
            reply_to=trying_to_upload_msg,
            caption=base_name)
    except:
        await event.reply("Julia encountered an exception. :(\n" +
                          traceback.format_exc())
    finally:
        await remove_potential_file(file_add, event)


async def safe_run(event, cwd, command):
    ## await event.reply('bash -c "' + command + '"' + '\n' + cwd)
    await pexpect_ai.run(command, cwd=cwd)

async def simple_run(event, cwd, command):
    ## await event.reply('bash -c "' + command + '"' + '\n' + cwd)
    await pexpect_ai.run('bash -c ' + shlex.quote(command) + '', cwd=cwd)


async def remove_potential_file(file, event=None):
    try:
        if await os_aio.path.exists(file) and await os_aio.path.isfile(file):
            await os_aio.remove(file)
    except:
        if event is not None:
            await event.reply("Julia encountered an exception. :(\n" +
                              traceback.format_exc())


async def discreet_send(event, message, reply_to, quiet, link_preview=False):
    if quiet:
        return reply_to
    else:

        return await event.respond(
            message, link_preview=link_preview, reply_to=reply_to)
        # return await borg.send_message(
        #     await event.get_chat(), message, link_preview=link_preview, reply_to=reply_to)
