from __future__ import unicode_literals

import re

import pexpect
from telethon import TelegramClient, events
from telethon.tl.functions.users import GetFullUserRequest
import logging
from requests import get  # to make GET request
import wget
import os
import sys
import traceback
import youtube_dl
import uuid
import types

is_interactive = True


######################
async def get_music(name='Halsey Control',
                    cwd="./dls/" + str(uuid.uuid4()) + "/",
                    tg_event=None):
    pexpect.run('mkdir -p ' + cwd)
    child = pexpect.spawn('instantmusic', cwd=cwd)
    child.logfile = open('/tmp/mylog', 'wb')
    child.expect('Enter*')
    child.sendline(name)
    child.expect(re.compile(b'Found.*Pick one', re.S))
    if tg_event is None:
        child.sendline('0')
    else:
        choose_msg = await tg_event.reply(child.match.group().decode('utf-8') +
                                          ".")
        choose_reply_msg = await await_reply(await tg_event.chat, choose_msg)
        choice_text = choose_reply_msg.raw_text
        if choice_text.isnumeric():
            child.sendline(choice_text)
        else:
            child.sendline('0')
    child.expect(['Download*', '\(y/n\)*'])
    child.sendline('y')
    child.expect(['Fixed*', 'couldnt get album art*'], timeout=240)
    return cwd + str(
        pexpect.run('bash -c "ls -a | grep mp3"', cwd=cwd), 'utf-8').strip()


######################
async def await_reply(chat, message):
    async def reply_filter(new_msg_event):
        return (new_msg_event.message.reply_to_msg_id == message.id)

    fut = borg.await_event(events.NewMessage(chats=chat), reply_filter)
    return await fut


######################
# orphic = await borg.get_entity('Orphicality')


@borg.on(events.NewMessage())
async def _(event):
    first_line = "l"
    try:
        first_line = event.raw_text.lower().splitlines().pop(0)
    except:
        # print(traceback.format_exc())
        first_line = "k"
    if await event.sender is not None:
        sender_full = await borg(GetFullUserRequest(await event.sender))
    # print(first_line)
    if ('ژاله' in first_line or 'زاله' in first_line or 'julia' in first_line):
        # print("Julia")
        global my_event
        my_event = event
        if await event.sender is not None and (
            (await event.sender).is_self or
            (await event.sender).username == "Orphicality"):
            if any(s in first_line for s in ('laugh', 'بخند')):
                await event.reply('😆')
            if any(s in first_line for s in ('you okay?','خوبی')):
                await event.reply('I know of no light. :p')


            if any(
                    s in first_line for s in ('nice work', 'thanks', 'merci',
                                              'good job', 'مرسی')):
                await event.reply("You're welcome. ❤️")
        # else:
        # else:

        if any(s in first_line for s in ('debug', 'دیباگ')):
            db_msg = await event.reply('DEBUG')
            db_reply = await await_reply(await event.chat, db_msg)
            print("YDebug: " + db_reply.raw_text)

        if any(
                s in first_line for s in ('hi', 'hello', 'hey', 'yo',
                                          'greetings', 'سلام', 'هی', 'یو!')):
            sender_name = "Unknown"
            if await event.sender is not None:
                sender_name = (await event.sender).first_name
            await event.reply("Julia is operational.\nGreetings,  " +
                              sender_name + "!")

        if any(s in first_line for s in ('upload', 'اپلود', 'آپلود')):
            urls = event.raw_text.splitlines()
            urls.pop(0)
            for url in urls:
                try:
                    if url == '':
                        continue
                    url_name = wget.detect_filename(url)
                    trying_to_dl_msg = await event.reply(
                        "Julia is trying to download \"" + url_name +
                        "\" from \"" + url + "\".\nPlease wait ...",
                        link_preview=False)
                    d1 = wget.download(url, out="dls/", bar=None)
                    try:
                        trying_to_upload_msg = await borg.send_message(
                            await event.chat, "Julia is trying to upload \"" +
                            url_name + "\".\nPlease wait ...",
                            trying_to_dl_msg)
                        await borg.send_file(
                            await event.chat,
                            d1,
                            reply_to=trying_to_upload_msg,
                            caption=(url_name))
                    except:
                        await event.reply(
                            "Julia encountered an exception. :(\n" +
                            traceback.format_exc())
                    finally:
                        await remove_potential_file(d1)

                except:
                    await event.reply("Julia encountered an exception. :(\n" +
                                      traceback.format_exc())

        if any(s in first_line for s in ('yt', 'youtube', 'یوتیوب')):
            urls = event.raw_text.splitlines()
            urls.pop(0)
            for url in urls:
                if url == '':
                    continue
                try:
                    # def my_hook(d):
                    #     if not hasattr(my_hook, 'some_static_var'):
                    #         my_hook.some_static_var = False
                    #     if d['status'] == 'finished' and not my_hook.some_static_var:
                    #         # my_hook.some_static_var = True
                    #         d1 = d['filename']
                    #         try:
                    #             await event.reply("Julia is trying to upload " + d1 +
                    #                         ".\nPlease wait ...")
                    #             await borg.send_file(
                    #                 await event.chat,
                    #                 d1,
                    #                 reply_to=await event.message,
                    #                 caption=(d1))
                    #         except:
                    #             await event.reply("Julia encountered an exception. :(\n" +
                    #                         traceback.format_exc())

                    trying_to_dl = await event.reply(
                        "Julia is trying to download \"" + url +
                        "\".\nPlease wait ...",
                        link_preview=False)
                    file_name = 'dls/' + str(uuid.uuid4())
                    ydl_opts = {
                        # 'progress_hooks': [my_hook],
                        'format':
                        'bestvideo[ext=mp4]+bestaudio[ext=m4a]',  # workaround to always get .mp4
                        'quiet': True,
                        'outtmpl':
                        file_name  # 'dls/%(playlist_title)s_%(title)s_%(format)s_%(autonumber)s.%(ext)s'
                    }
                    with youtube_dl.YoutubeDL(ydl_opts) as ydl:
                        d2 = ydl.extract_info(url)
                        file_name_with_ext = file_name + "." + "mp4"  # + d2['ext']
                        trying_to_upload_msg = await borg.send_message(
                            await event.chat,
                            "Julia is trying to upload \"" + d2['title'] +
                            "\".\nPlease wait ...",
                            trying_to_dl.id,
                            link_preview=False)
                        sent_video = await borg.send_file(
                            await event.chat,
                            file_name_with_ext,
                            reply_to=trying_to_upload_msg,
                            caption=str(d2['title']))
                        full_caption = "Title: " + str(
                            d2['title']
                        ) + "\nFormat: " + str(
                            d2['format']
                        ) + "\nWidth: " + str(d2['width']) + "\nHeight: " + str(
                            d2['height']
                        ) + "\nFPS: " + str(d2['fps']) + "\nPlaylist: " + str(
                            d2['playlist']) + "\nLikes: " + str(
                                d2['like_count']) + "\nDislikes: " + str(
                                    d2['dislike_count']
                                ) + "\nView Count: " + str(
                                    d2['view_count']) + "\nUploader: " + str(
                                        d2['uploader'] + "\nWebpage Url: " +
                                        str(d2['webpage_url']) +
                                        "\nDescription:\n" +
                                        str(d2['description']))
                        await borg.send_message(
                            await event.chat,
                            full_caption,
                            sent_video.id,
                            link_preview=False)

                except:
                    await event.reply("Julia encountered an exception. :(\n" +
                                      traceback.format_exc())
                finally:
                    await remove_potential_file(file_name_with_ext)
        if any(
                s in first_line
                for s in ('music', 'موسیقی', 'اهنگ', 'آهنگ')):
            # print(first_line)
            urls = event.raw_text.splitlines()
            urls.pop(0)
            for url in urls:
                # print(url)
                if url == '':
                    continue
                file_name_with_ext = ''
                trying_to_dl = await event.reply(
                    "Julia is trying to download \"" + url +
                    "\".\nPlease wait ...",
                    link_preview=False)
                try:
                    if any(
                            s in first_line
                            for s in ('automatic', 'اتوماتیک')):
                        file_name_with_ext = await get_music(url)
                    else:
                        file_name_with_ext = await get_music(
                            url, tg_event=event)
                    base_name = str(os.path.basename(file_name_with_ext))
                    trying_to_upload_msg = await borg.send_message(
                        await event.chat,
                        "Julia is trying to upload \"" + base_name +
                        "\".\nPlease wait ...",
                        trying_to_dl,
                        link_preview=False)
                    sent_music = await borg.send_file(
                        await event.chat,
                        file_name_with_ext,
                        reply_to=trying_to_upload_msg,
                        caption=base_name)
                except:
                    await event.reply(
                        "Julia encountered an exception. :(\n" +
                        traceback.format_exc())
                finally:
                    await remove_potential_file(file_name_with_ext, event)


async def remove_potential_file(file, event=None):
    try:
        if os.path.exists(file) and os.path.isfile(file):
            os.remove(file)
    except:
        if event is not None:
            await event.reply("Julia encountered an exception. :(\n" +
                              traceback.format_exc())

async def discreet_send(event,message,reply_to,quiet):
    if quiet:
        return reply_to
    else:
        await borg.send_message(await event.chat)
