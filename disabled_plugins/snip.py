# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.
from telethon import events, utils
from telethon.tl import types


TYPE_TEXT = 0
TYPE_PHOTO = 1
TYPE_DOCUMENT = 2


# {name: {'text': text, 'id': id, 'hash': access_hash, 'type': type}}
snips = storage.snips or {}


@borg.on(events.NewMessage(pattern=r"\.snip (\S+)", outgoing=True))
async def on_snip(event):
    name = event.pattern_match.group(1)
    if name not in snips:
        await on_snip_save(event)
    else:
        snip = snips[name]
        if snip["type"] == TYPE_PHOTO:
            media = types.InputPhoto(snip["id"], snip["hash"])
        elif snip["type"] == TYPE_DOCUMENT:
            media = types.InputDocument(snip["id"], snip["hash"])
        else:
            media = None

        await borg.send_message(
            await event.get_input_chat(),
            snip["text"],
            file=media,
            reply_to=event.message.reply_to_msg_id,
        )

    await event.delete()


@borg.on(events.NewMessage(pattern=r"\.snips (\S+)", outgoing=True))
async def on_snip_save(event):
    name = event.pattern_match.group(1)
    msg = await event.get_reply_message()
    if msg:
        snips.pop(name, None)
        snip = {"type": TYPE_TEXT, "text": msg.message or ""}
        if msg.media:
            media = None
            if isinstance(msg.media, types.MessageMediaPhoto):
                media = utils.get_input_photo(msg.media.photo)
                snip["type"] = TYPE_PHOTO
            elif isinstance(msg.media, types.MessageMediaDocument):
                media = utils.get_input_document(msg.media.document)
                snip["type"] = TYPE_DOCUMENT
            if media:
                snip["id"] = media.id
                snip["hash"] = media.access_hash

        snips[name] = snip
        storage.snips = snips

    await event.delete()


@borg.on(events.NewMessage(pattern=r"\.snipl", outgoing=True))
async def on_snip_list(event):
    await event.respond("available snips: " + ", ".join(snips.keys()))
    await event.delete()


@borg.on(events.NewMessage(pattern=r"\.snipd (\S+)", outgoing=True))
async def on_snip_delete(event):
    snips.pop(event.pattern_match.group(1), None)
    storage.snips = snips
    await event.delete()


@borg.on(events.NewMessage(pattern=r"\.snipr (\S+)\s+(\S+)", outgoing=True))
async def on_snip_rename(event):
    snip = snips.pop(event.pattern_match.group(1), None)
    if snip:
        snips[event.pattern_match.group(2)] = snip
        storage.snips = snips

    await event.delete()
