from telethon import TelegramClient, events
from uniborg import util
from IPython import embed


@borg.on(events.NewMessage(pattern=r"(?i)^\.ie\s+((?:.|\n)*)$"))
async def _(event):
    if await util.isAdmin(event) and event.message.forward == None:
        await event.reply(str(await util.saexec(event.pattern_match.group(1), borg=borg, event=event)))
#        exec(
#            f'async def __ex(): ' +
#            ''.join(f'\n {l}' for l in event.pattern_match.group(1).split('\n'))
#        )
#
    # Get `__ex` from local variables, call it and return the result
#        await locals()['__ex']()
