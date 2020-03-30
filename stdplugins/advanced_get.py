from telethon import TelegramClient, events
from pathlib import Path
import uuid
import subprocess
from uniborg import util
from IPython import embed
import re

p = re.compile(r"(?im)^\.a(n?)\s+((?:.|\n)*) fin$")
    
@borg.on(events.NewMessage(pattern=r"(?im)^\.a(n?)\s+((?:.|\n)*)$"))
async def _(event):
    #print("aget received")
    if await util.isAdmin(event) and event.message.forward == None:
        await util.aget(event)

@borg.on(events.InlineQuery)
async def handler(event):
    query = event.text.lower()
    m = p.match(query)
    if (not await util.isAdmin(event)) or m == None:
        #print("inline rejected: " + query)
        #util.ix()
        #embed(using='asyncio')
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

    result = builder.article('aget', text=output, link_preview=False)

    # NOTE: You should always answer, but we want plugins to be able to answer
    #       too (and we can only answer once), so we don't always answer here.
    if result:
        await event.answer([result]) #returns true
        # util.ix()
        # embed(using='asyncio')

