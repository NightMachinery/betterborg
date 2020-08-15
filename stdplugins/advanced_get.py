from telethon import TelegramClient, events
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

p = re.compile(r"(?im)^\.a(n?)\s+((?:.|\n)*) fin$")
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


@borg.on(events.InlineQuery)
async def handler(event):
    query = event.text  # .lower()
    m = p.match(query)
    if (not await util.isAdmin(event)) or m == None:
        #print("inline rejected: " + query)
        # util.ix()
        # embed(using='asyncio')
        return
    print("inline accepted: " + query)
    builder = event.builder
    #result = builder.article('aget', text=m.group(2), link_preview=False)
    command = m.group(2)
    shell = True
    cwd = util.dl_base + "Inline " + str(uuid.uuid4()) + '/'
    Path(cwd).mkdir(parents=True, exist_ok=True)
    sp = (subprocess.run(command,
                         shell=shell,
                         cwd=cwd,
                         text=True,
                         executable='zsh' if shell else None,
                         stderr=subprocess.STDOUT,
                         stdout=subprocess.PIPE))
    output = sp.stdout
    output = f"The process exited {sp.returncode}." if output == '' else output

    rtext = builder.article('Text', text=output, link_preview=False)
    rfiles = [rtext]
    files = list(Path(cwd).glob('*'))
    files.sort()
    for f in files:
        if not f.is_dir():  # and not any(s in p.name for s in ('.torrent', '.aria2')):
            file_add = f.absolute()
            base_name = str(os.path.basename(file_add))
            ext = f.suffix
            # embed()
            # if ext == '.mp3' or ext == '.m4a' or ext == '.m4b':
            #file_add = 'http://82.102.11.148:8080//tmp/Pharrell%20Williams%20-%20Despicable%20Me.c.c.mp3'
            #rfiles.append(builder.document(file_add, type='audio'))
            # rfiles.append(builder.document(file_add, type='document', text='hi 8')) #, title=base_name, description='test 36'))

    # NOTE: You should always answer, but we want plugins to be able to answer
    #       too (and we can only answer once), so we don't always answer here.
    await util.remove_potential_file(cwd, None)
    await event.answer(rfiles, cache_time=0, private=True)  # returns true
    # util.ix()
    # embed(using='asyncio')


@borg.on(util.admin_cmd(pattern=".xf"))
async def _(event):
    util.init_brishes()
    await event.reply("Reinitialized brishes. Note that old running instances can still rejoin.")

@borg.on(util.admin_cmd(pattern=".(x|sbb)"))
async def _(event):
    util.restart_brishes()
    await event.reply("Restarted brishes.")
