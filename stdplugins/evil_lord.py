from __future__ import unicode_literals
from IPython import embed

# TODO Send try_dl of music after getting number of track if not automatic
# TODO aioify blocking calls
# TODO irs music dler
# TODO torrent
# TODO aria2 dler
# TODO Go through WG-TE and add the spotify songs based on today's date: 8 Jun 2018
# TODO repeat n-times module
# TODO self_only eval
# TODO Tumblr? :D
# TODO Google?
# TODO adapt other modules
# TODO borg injection into util

from aioify import aioify
import eyed3
import re

import asyncio
import pexpect
from telethon import TelegramClient, events
from telethon.tl.functions.users import GetFullUserRequest
import logging
from uniborg import util
from requests import get  # to make GET request
import wget
import os
import sys
import traceback
import youtube_dl
import uuid
import types

is_interactive = True

pexpect_ai = aioify(pexpect)
os_aio = aioify(os)
eyed3_aio = aioify(eyed3)

######################
async def get_music(name="Halsey Control", cwd="./dls/BAD/", tg_event=None):
    # Be sure to set cwd again. It is only set once.
    # TODO Raise exception if cwd is not set. Non-optional named variable?

    # print(name + " cwd0: \n" + cwd)
    await pexpect_ai.run("mkdir -p " + cwd)
    child = await pexpect_ai.spawn("instantmusic", cwd=cwd)
    child.logfile = open("/tmp/mylog", "wb")
    child.expect("Enter*")
    child.sendline(name)
    child.expect(re.compile(b"Found.*Pick one", re.S))
    if tg_event is None:
        child.sendline("0")
    else:
        choose_msg = await tg_event.reply(child.match.group().decode("utf-8") + ".")
        choose_reply_msg = await await_reply(await tg_event.get_chat(), choose_msg)
        choice_text = choose_reply_msg.raw_text
        if choice_text.isnumeric():
            child.sendline(choice_text)
        else:
            child.sendline("0")
    child.expect(["Download*", "\(y/n\)*"])
    child.sendline("y")
    # print(name + " cwd1: \n" + cwd)
    await (
        aioify(child.expect)(
            # ['Fixed*', 'couldnt get album art*'], timeout=3000))
            ["Deleting*(pass -k to keep)*", pexpect.EOF],
            timeout=3000,
        )
    )
    # print(name + " cwd2: \n" + cwd)
    mp3_file_add = (
        cwd
        + str(
            await pexpect_ai.run('bash -c "exa -a --color=never"', cwd=cwd), "utf-8"
        ).strip()
    )  # exa --all doesn't include . and ..
    # embed()
    mp3_file = await eyed3_aio.load(mp3_file_add)
    mp3_file.tag.title = str(await os_aio.path.basename(mp3_file_add))[:-4]
    await (aioify(mp3_file.tag.save))()
    return mp3_file_add


######################
async def await_reply(chat, message):
    async def reply_filter(new_msg_event):
        return new_msg_event.message.reply_to_msg_id == message.id

    fut = borg.await_event(events.NewMessage(chats=chat), reply_filter)
    return await fut


######################
@borg.on(events.NewMessage())
async def _(event):
    first_line = "l"
    try:
        first_line = event.raw_text.lower().splitlines().pop(0)
    except:
        pass
    quiet = any(s in first_line for s in ("quiet", "ساکت", "آروم", "اروم"))
    if "ژاله" in first_line or "زاله" in first_line or "julia" in first_line:
        # print("Julia")
        global my_event
        my_event = event
        sender = await event.message.get_sender()
        if sender is not None and (
            event.sender.username == un for un in ("Arstar", "Untethered")
        ):
            if any(s in first_line for s in ("laugh", "بخند")):
                await event.reply("😆")
            if any(s in first_line for s in ("you okay", "خوبی")):
                await event.reply("I know of no light. :p")

            if any(
                s in first_line
                for s in ("nice work", "thanks", "merci", "good job", "مرسی")
            ):
                await event.reply("You're welcome. ❤️")
        # else:
        # else:

        if any(s in first_line for s in ("debug", "دیباگ")):
            db_msg = await event.reply("DEBUG")
            db_reply = await await_reply(await event.get_chat(), db_msg)
            print("YDebug: " + db_reply.raw_text)

        if any(
            s in first_line
            for s in ("hi", "hello", "hey", "yo", "greetings", "سلام", "هی", "یو!")
        ):
            sender_name = "Unknown"
            if event.sender is not None:
                sender_name = getattr(event.sender, "first_name", "X")
            await event.reply("Julia is operational.\nGreetings,  " + sender_name + "!")

        if any(s in first_line for s in ("upload", "اپلود", "آپلود")):
            urls = event.raw_text.splitlines()
            urls.pop(0)
            for url in urls:
                try:
                    if url == "":
                        continue
                    url_name = wget.detect_filename(url)
                    trying_to_dl_msg = await util.discreet_send(
                        event,
                        'Julia is trying to download "'
                        + url_name
                        + '" from "'
                        + url
                        + '".\nPlease wait ...',
                        event.message,
                        quiet,
                    )
                    d1 = wget.download(url, out="dls/", bar=None)
                    try:
                        trying_to_upload_msg = await util.discreet_send(
                            event,
                            'Julia is trying to upload "'
                            + url_name
                            + '".\nPlease wait ...',
                            trying_to_dl_msg,
                            quiet,
                        )
                        await borg.send_file(
                            await event.get_chat(),
                            d1,
                            reply_to=trying_to_upload_msg,
                            caption=(url_name),
                        )
                    except:
                        await event.reply(
                            "Julia encountered an exception. :(\n"
                            + traceback.format_exc()
                        )
                    finally:
                        await util.remove_potential_file(d1)

                except:
                    await event.reply(
                        "Julia encountered an exception. :(\n" + traceback.format_exc()
                    )

        if any(s in first_line for s in ("yt", "youtube", "یوتیوب")):
            urls = event.raw_text.splitlines()
            urls.pop(0)
            for url in urls:
                if url == "":
                    continue
                file_name_with_ext = ""
                try:
                    trying_to_dl = await util.discreet_send(
                        event,
                        'Julia is trying to download "' + url + '".\nPlease wait ...',
                        event.message,
                        quiet,
                    )
                    file_name = "dls/" + str(uuid.uuid4()) + "/"
                    ydl_opts = {
                        "quiet": True,
                        "outtmpl": file_name
                        + "%(playlist_title)s_%(title)s_%(format)s.%(ext)s",  # 'dls/%(playlist_title)s_%(title)s_%(format)s_%(autonumber)s.%(ext)s'
                    }
                    with youtube_dl.YoutubeDL(ydl_opts) as ydl:
                        extract_info_aio = aioify(ydl.extract_info)
                        d2 = await extract_info_aio(url)
                        file_name_with_ext = (
                            file_name + (await os_aio.listdir(file_name))[0]
                        )
                        trying_to_upload_msg = await util.discreet_send(
                            event,
                            'Julia is trying to upload "'
                            + d2["title"]
                            + '".\nPlease wait ...',
                            trying_to_dl,
                            quiet,
                        )
                        sent_video = await borg.send_file(
                            await event.get_chat(),
                            file_name_with_ext,
                            reply_to=trying_to_upload_msg,
                            caption=str(d2["title"]),
                        )
                        try:
                            full_caption = (
                                "Title: "
                                + str(d2["title"])
                                + "\nFormat: "
                                + str(d2["format"])
                                + "\nWidth: "
                                + str(d2["width"])
                                + "\nHeight: "
                                + str(d2["height"])
                                + "\nFPS: "
                                + str(d2["fps"])
                                + "\nPlaylist: "
                                + str(d2["playlist"])
                                + "\nLikes: "
                                + str(d2["like_count"])
                                + "\nDislikes: "
                                + str(d2["dislike_count"])
                                + "\nView Count: "
                                + str(d2["view_count"])
                                + "\nUploader: "
                                + str(
                                    d2["uploader"]
                                    + "\nWebpage Url: "
                                    + str(d2["webpage_url"])
                                    + "\nDescription:\n"
                                    + str(d2["description"])
                                )
                            )
                            await borg.send_message(
                                await event.get_chat(),
                                full_caption,
                                sent_video,
                                link_preview=False,
                            )
                        except:
                            pass
                except:
                    await event.reply(
                        "Julia encountered an exception. :(\n" + traceback.format_exc()
                    )
                finally:
                    await util.remove_potential_file(file_name_with_ext)
        if any(s in first_line for s in ("music", "موسیقی", "اهنگ", "آهنگ")):
            # print(first_line)
            urls = event.raw_text.splitlines()
            urls.pop(0)
            for url in urls:
                # print(url)
                if url == "":
                    continue
                file_name_with_ext = ""
                trying_to_dl = await util.discreet_send(
                    event,
                    'Julia is trying to download "' + url + '".\nPlease wait ...',
                    event.message,
                    quiet,
                )
                try:
                    if any(s in first_line for s in ("automatic", "اتوماتیک")):
                        file_name_with_ext = await get_music(
                            url, cwd="./dls/" + str(uuid.uuid4()) + "/"
                        )
                    else:
                        file_name_with_ext = await get_music(
                            url, tg_event=event, cwd="./dls/" + str(uuid.uuid4()) + "/"
                        )
                    base_name = str(await os_aio.path.basename(file_name_with_ext))
                    trying_to_upload_msg = await util.discreet_send(
                        event,
                        'Julia is trying to upload "'
                        + base_name
                        + '".\nPlease wait ...',
                        trying_to_dl,
                        quiet,
                    )
                    sent_music = await borg.send_file(
                        await event.get_chat(),
                        file_name_with_ext,
                        reply_to=trying_to_upload_msg,
                        caption=base_name,
                    )
                except:
                    await event.reply(
                        "Julia encountered an exception. :(\n" + traceback.format_exc()
                    )
                finally:
                    await util.remove_potential_file(file_name_with_ext, event)
    p = re.compile(r'^Added to (.*) on Spotify: "(.*)" by (.*) https:.*$')
    m = p.match(event.raw_text)
    if m is not None:
        file_name_with_ext = ""
        try:
            # print(m.group(3)+" "+m.group(2)) #DBG
            file_name_with_ext = await get_music(
                m.group(3) + " " + m.group(2), cwd="./dls/" + str(uuid.uuid4()) + "/"
            )
            base_name = str(await os_aio.path.basename(file_name_with_ext))
            sent_music = await borg.send_file(
                await event.get_chat(),
                file_name_with_ext,
                reply_to=event.message,
                caption=base_name,
            )
        except:
            await event.reply(
                "Julia encountered an exception. :(\n" + traceback.format_exc()
            )
        finally:
            await util.remove_potential_file(file_name_with_ext, event)
