from telethon import TelegramClient, events
from uniborg import util
from IPython import embed


@borg.on(events.NewMessage(pattern=r"(?i)^\.s (.*)$"))
async def _(event):
    if event.message.forward == None:
        await util.aget(event, ("spotdl", event.pattern_match.group(1)), False)
        # await util.aget(event, ('spotify.zsh', event.pattern_match.group(1)), False)
        # await util.aget(event, ('spotdl', '-f', '.', '-s', event.pattern_match.group(1)), False)


# @borg.on(events.NewMessage(pattern=r"(?i)^\.sb+ (.*)$"))
# async def _(event):
#     if event.message.forward == None:
#         await util.aget(event, ('spotify.zsh', '-a', event.pattern_match.group(1)), False)


# @borg.on(events.NewMessage(pattern=r"(?i)^\.sp+ (.*)$"))
# async def _(event):
#     if event.message.forward == None:
#         await util.aget(event, ('spotify.zsh', '-p', event.pattern_match.group(1)), False)
