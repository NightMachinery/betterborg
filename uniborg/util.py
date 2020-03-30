# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.

from aioify import aioify
from functools import partial
import asyncio
import subprocess
import uuid
import traceback
import os
import pexpect
import shlex
import re
import shutil
from uniborg import util
from telethon import TelegramClient, events
from telethon.tl.functions.messages import GetPeerDialogsRequest
from IPython import embed
from pathlib import Path

dl_base = 'dls/'
#pexpect_ai = aioify(obj=pexpect, name='pexpect_ai')
pexpect_ai = aioify(pexpect)
#os_aio = aioify(obj=os, name='os_aio')
os_aio = aioify(os)
#subprocess_aio = aioify(obj=subprocess, name='subprocess_aio')
subprocess_aio = aioify(subprocess)
borg = None


def admin_cmd(pattern):
    return events.NewMessage(outgoing=True, pattern=re.compile(pattern))
    # return events.NewMessage(chats=admins, pattern=re.compile(pattern))


def interact(local=None):
    if local is None:
        local = locals()
    import code
    code.interact(local=local)


def ix():
    import nest_asyncio
    nest_asyncio.apply()


async def isAdmin(
        event,
        admins=("Orphicality", ),
        adminChats=("https://t.me/joinchat/AAAAAERV9wGWQKOF5hgQSA", )):
    chat = await event.get_chat()
    msg = getattr(event, 'message', None)
    sender = getattr(msg, 'sender', getattr(event, 'sender', None))
    #Doesnt work with private channels' links
    res = (getattr(msg, 'out', False)) or (chat.id in adminChats)  or (getattr(chat, 'username', 'NA') in adminChats) or (
        sender is not None and
        (getattr(sender, 'is_self', False) or
         (sender).username in admins))
    # ix()
    # embed(using='asyncio')
    return res


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
    # await pexpect_ai.run('bash -c "mkdir -p ' + cwd + '"')
    Path(cwd).mkdir(parents=True, exist_ok=True)
    # util.interact(locals())
    a = borg
    rep_id = event.message.reply_to_msg_id
    dled_file_name = ''
    dled_path = ''
    dled_exists = False
    if rep_id != None:
        z = await a.get_messages(event.chat, ids=rep_id)
        # await z.download_media()
        # ix()
        # embed(using='asyncio')
        if z.file != None:
            dled_file_name = getattr(z.file, 'name', 'some_file')
            dled_file_name = 'some_file' if dled_file_name == '' or dled_file_name == None else dled_file_name
            dled_path = cwd + dled_file_name
            dled_path = await a.download_media(message=z, file=dled_path)
            dled_exists = True
    mdate = os.path.getmtime(dled_path) if dled_exists else ""
    await to_await(cwd=cwd, event=event)
    if dled_exists and os.path.exists(dled_path) and mdate == os.path.getmtime(dled_path):
        await remove_potential_file(dled_path, event)
    # return cwd + str(
    # await pexpect_ai.run('bash -c "ls -p | grep -E -v \'/|\.aria2.*|\.torrent$\'"', cwd=cwd),
    # 'utf-8').strip()
    return cwd


async def run_and_upload(event, to_await, quiet=True):
    file_add = ''
    # util.interact(locals())
    try:
        chat = await event.get_chat()
        try:
            await borg.send_read_acknowledge(chat, event.message)
        except:
            pass
        trying_to_dl = await util.discreet_send(
            event, "Julia is processing your request ...", event.message,
            quiet)
        cwd = await run_and_get(event=event, to_await=to_await)
        #client = borg
        files = list(Path(cwd).glob('*'))
        files.sort()
        for p in files:
            if not p.is_dir(
            ):  # and not any(s in p.name for s in ('.torrent', '.aria2')):
                file_add = p.absolute()
                base_name = str(await os_aio.path.basename(file_add))
                # trying_to_upload_msg = await util.discreet_send(
                # event, "Julia is trying to upload \"" + base_name +
                # "\".\nPlease wait ...", trying_to_dl, quiet)
                voice_note = base_name.startswith('voicenote-')
                video_note = base_name.startswith('videonote-')
                force_doc = base_name.startswith('fdoc-')
                supports_streaming = base_name.startswith('streaming-')
                async with borg.action(chat,'document') as action:
                    await borg.send_file(chat, file_add, voice_note=voice_note, video_note=video_note, supports_streaming=supports_streaming, 
                                                     force_document=force_doc,
                                                     reply_to=event.message,
                                                     allow_cache=False)
                         #                            progress_callback=action.progress)
                # caption=base_name)
    except:
        await event.reply("Julia encountered an exception. :(\n" +
                          traceback.format_exc())
    finally:
        await remove_potential_file(cwd, event)


async def safe_run(event, cwd, command):
    ## await event.reply('bash -c "' + command + '"' + '\n' + cwd)
    #await pexpect_ai.run(command, cwd=cwd)
    await subprocess_aio.run(command, cwd=cwd)


async def simple_run(event, cwd, command, shell=True):
    sp = (await subprocess_aio.run(command,
                                       shell=shell,
                                       cwd=cwd,
                                       text=True,
                                       executable='zsh' if shell else None,
                                       stderr=subprocess.STDOUT,
                                       stdout=subprocess.PIPE))
    output = sp.stdout
    output = f"The process exited {sp.returncode}." if output == '' else output
    if not shell:
        print(output)
        if sp.returncode != 0:
            output="Something went wrong."
        else:
            output=''
    await discreet_send(event, output, event.message)


async def remove_potential_file(file, event=None):
    try:
        if await os_aio.path.exists(file):
            if await os_aio.path.isfile(file):
                await os_aio.remove(file)
            else:
                shutil.rmtree(file)  #awaitable
    except:
        if event is not None:
            await event.reply("Julia encountered an exception. :(\n" +
                              traceback.format_exc())


async def discreet_send(event, message, reply_to, quiet=False, link_preview=False):
    if quiet or len(message) == 0:
        return reply_to
    else:
        s = 0
        e = 4000
        while (len(message) > s):
            last_msg = await event.respond(message[s:e],
                                           link_preview=link_preview,
                                           reply_to=(reply_to if s == 0 else last_msg))
            s = e
            e = s + 4000
        return last_msg
async def saexec(code, **kwargs):
    # Don't clutter locals
    locs = {}
    args = ", ".join(list(kwargs.keys()))
    exec(f"async def func({args}):\n    " + code.replace("\n", "\n    "), {}, locs)
    # Don't expect it to return from the coro.
    result = await locs["func"](**kwargs)
    return result
async def aget(event, command='', shell=True, match=None):
    if match == None:
        match = event.pattern_match
    if command == '':
        command = match.group(2).replace("‘","'").replace('“','"').replace("’","'").replace('”','"').replace('—','--')
        if match.group(1) == 'n':
            command = 'noglob ' + command
    await util.run_and_upload(
        event=event,
        to_await=partial(util.simple_run, command=command, shell=shell))
