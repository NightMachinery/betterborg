#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""
First, a few handler functions are defined. Then, those functions are passed to
the Dispatcher and registered at their respective places.
Then, the bot is started and runs until we press Ctrl-C on the command line.
Usage:
Basic inline bot example. Applies different text transformations.
Press Ctrl-C on the command line or send a signal to the process to stop the
bot.
"""
import logging
import os
import json
from pathlib import Path
from typing import Dict, Iterable
from IPython import embed
from brish import z, zp, bsh, zq
from uuid import uuid4
import re
from cachetools import cached, LRUCache, TTLCache
from threading import RLock
import traceback

from telegram import InlineQueryResultArticle, ParseMode, \
    InputTextMessageContent, InlineQueryResultCachedDocument, InlineQueryResultCachedVideo, InlineQueryResultCachedGif, InlineQueryResultCachedMpeg4Gif, InlineQueryResultCachedPhoto, InlineQueryResultCachedVoice, InlineQueryResultPhoto, InlineQueryResultVideo
from telegram import parsemode
from telegram.ext import Updater, InlineQueryHandler, CommandHandler
from telegram.utils.helpers import DEFAULT_NONE, escape_markdown

##
# Enable logging
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
                    level=logging.INFO)

logger = logging.getLogger(__name__)
##
MAX_LENGTH = 4050
# https://stackoverflow.com/questions/46011661/how-to-send-large-size-of-the-caption-on-telegram-bot-using-c
MEDIA_MAX_LENGTH = 1000
PAF = re.compile(r"(?im)^(?:\.a(n?)\s+)?((?:.|\n)*)\s+fin$")
PDI = re.compile(r"(?im)^\.di\s+(\S+)(?:\s+(\S*))?\s+fin$")
PC_KITSU = re.compile(r"(?im)^\.ki\s+(.+)$")
PC_GOO = re.compile(r"(?im)^\.g\s+(.+)$")
WHITESPACE = re.compile(r"^\s*$")
dl_base = os.getcwd() + '/dls/'
##
# @todoc A throwaway group/channel for storing files. (I use TMPC.)
tmp_chat = int(z('ecn "${{borg_tmpc:--1001215308649}}"').outrs)
#-1001496131468 (old TMPC)
##
lock_inline = RLock()
##


def start(update, context):
    """Send a message when the command /start is issued."""
    update.message.reply_text('Hi!')


def help_command(update, context):
    """Send a message when the command /help is issued."""
    update.message.reply_text('Help!')



admins = [195391705, ]
if z('test -n "$borg_admins"'):
    admins = admins + list(z("arr0 ${{(s.,.)borg_admins}}"))
graylist = [467602588, 92863048,
            90821188, 915098299, 665261327, 91294899, 1111288832]
graylist = admins + graylist

def isAdmin(update, admins=admins):
    # print(f"id: {update.effective_user.id}")
    # print(f"u: {update.effective_user.username}")
    # print(f"admins: {admins}")

    res = False
    try:
        # embed()
        res = (update.effective_user.id in admins) or (getattr(update.effective_user, 'username', None) in admins)
    except:
        res = False
    return res


def inlinequery(update, context):
    """Handle the inline query."""
    ###
    # https://python-telegram-bot.readthedocs.io/en/stable/telegram.inlinequery.html
    ##
    # There is pretty much no info except the sender in update. context is also useless. So we can't get the replied-to file.
    ###
    def ans_text(text: str = "", cache_time=1):  # "To undo the folded lie,"
        if not text:
            return
        update.inline_query.answer([InlineQueryResultArticle(
            id=uuid4(),
            title=text,
            input_message_content=InputTextMessageContent(text, disable_web_page_preview=False))], cache_time=cache_time, is_personal=True)

    if (not isAdmin(update, admins=graylist)):
        ans_text("""Defenceless under the night
Our world in stupor lies;
Yet, dotted everywhere,
Ironic points of light
Flash out wherever the Just
Exchange their messages:
May I, composed like them
Of Eros and of dust,
Beleaguered by the same
Negation and despair,
Show an affirming flame.
    - Auden""", cache_time=86400)
        return

    with lock_inline:
        query = update.inline_query.query
        m = PDI.match(query)
        if m:
            c_id = m.group(1)
            c_kind = m.group(2) or ''
            print(f"Download ID: {c_id} {c_kind}")
            result = None
            if c_kind == '':
                result = InlineQueryResultCachedDocument(
                    id=uuid4(),
                    title=str(c_id),
                    document_file_id=c_id)
            elif c_kind.startswith('vid'):
                result = InlineQueryResultCachedVideo(
                    id=uuid4(),
                    title=str(c_id),
                    video_file_id=c_id)
            elif c_kind == 'photo':
                result = InlineQueryResultCachedPhoto(
                    id=uuid4(),
                    title=str(c_id),
                    photo_file_id=c_id)
            elif c_kind == 'gif':
                result = InlineQueryResultCachedMpeg4Gif(
                    id=uuid4(),
                    title=str(c_id),
                    mpeg4_file_id=c_id)
            if result:
                try:
                    update.inline_query.answer(
                        [result], cache_time=1, is_personal=True)
                except:
                    ans_text(traceback.format_exc())
            else:
                ans_text(f"Invalid kind: {c_kind}")
            return
        command = ''
        cache_time = 1
        is_personal = True
        no_match = True
        m = PC_KITSU.match(query)
        if m:
            no_match = False
            arg = zq(str(m.group(1)))
            if not arg:
                ans_text()
                return
            command = f"kitsu-getall {arg}"
            cache_time = 86400
            is_personal = False
        m = PC_GOO.match(query)
        if m:
            no_match = False
            arg = zq(str(m.group(1)))
            if not arg:
                ans_text()
                return
            command = f"jigoo {arg}"
            is_personal = False
        if no_match:
            if (not isAdmin(update)):
                ans_text("""The enlightenment driven away,
The habit-forming pain,
Mismanagement and grief:
We must suffer them all again. - Auden""")
                return
            if query == ".x":
                bsh.restart()
                cache.clear()
                ans_text("Restarted")
                return
            m = PAF.match(query)
            if m == None:
                ans_text()
                return
            command = m.group(2)
            if m.group(1) == 'n':
                # embed()
                command = 'noglob ' + command
        if not command:
            ans_text()
            return
        print(f"Inline command accepted: {command}")
        results = get_results(command)
        update.inline_query.answer(
            results, cache_time=cache_time, is_personal=is_personal)


cache = TTLCache(maxsize=256, ttl=3600)


@cached(cache)
def get_results(command: str, json_mode: bool = True):
    cwd = dl_base + "Inline " + str(uuid4()) + '/'
    Path(cwd).mkdir(parents=True, exist_ok=True)
    res = z("""
    if cd {cwd} ; then
        {command:e}
    else
        echo Inline Query: cd failed >&2
    fi
    """, fork=True)  # @design We might want to add a timeout here. We also need a file cacher ... Easier yet, just cache the results array and reset it using our 'x' command
    out = res.outerr
    if WHITESPACE.match(out):
        out = f"The process exited {res.retcode}."
    out_j = None
    results = []
    if json_mode:
        try:
            out_j = json.loads(res.out)
        except:
            pass
    if out_j and not isinstance(out_j, str) and isinstance(out_j, Iterable):
        print(res.err)
        if True:
            for item in out_j:
                if isinstance(item, dict):
                    tlg_title = item.get("tlg_title", "")
                    tlg_preview = bool(item.get("tlg_preview", "y"))
                    tlg_video = item.get("tlg_video", "")
                    # Mime type of the content of video url, “text/html” or “video/mp4”.
                    tlg_video_mime = item.get("tlg_video_mime", "video/mp4")
                    tlg_img = item.get("tlg_img", "")
                    tlg_img_thumb = item.get("tlg_img_thumb", "") or tlg_img
                    tlg_content = item.get("tlg_content", item.get("caption", ""))
                    tlg_parsemode = item.get("tlg_parsemode", "").lower()
                    pm = DEFAULT_NONE
                    if tlg_parsemode == "md2":
                        pm = ParseMode.MARKDOWN_V2
                    elif tlg_parsemode == "md":
                        pm = ParseMode.MARKDOWN
                    elif tlg_parsemode == "html":
                        pm = ParseMode.HTML
                    print(f"Parse mode: {pm}, preview: {tlg_preview}")
                    if tlg_img:
                        # There is a bug that makes, e.g., `@spiritwellbot kitsu-getall moon 2 fin` show only two returned results, even though we return 10 results. Idk what's the cause.
                        print(
                            f"tlg_img found: {tlg_title}: {tlg_img} , {tlg_img_thumb}")
                        results.append(
                            InlineQueryResultPhoto(
                                id=uuid4(),
                                photo_url=tlg_img,
                                thumb_url=tlg_img_thumb,
                                title=f"{tlg_title}",
                                caption=tlg_content[:MEDIA_MAX_LENGTH],
                                parse_mode=pm)
                        )
                    elif tlg_video:
                        # test @spiritwellbot ec '[{"tlg_title":"f","tlg_video":"https://files.lilf.ir/tmp/Tokyo%20Ghoul%20AMV%20-%20Run-rVed44_uz8s.mp4"}]' fin
                        print(f"tlg_video found: {tlg_title}: {tlg_video}")
                        results.append(
                            InlineQueryResultVideo(
                                id=uuid4(),
                                video_url=tlg_video,
                                mime_type=tlg_video_mime,
                                # To bypass telegram.error.BadRequest: Video_thumb_url_empty
                                thumb_url=(
                                    tlg_img_thumb or "https://media.kitsu.io/anime/cover_images/3936/original.jpg?1597696323"),
                                title=f"{tlg_title}",
                                caption=tlg_content[:MEDIA_MAX_LENGTH],
                                parse_mode=pm))
                    elif tlg_title:
                        print(f"tlg_title found: {tlg_title}")
                        results.append(
                            InlineQueryResultArticle(
                                id=uuid4(),
                                title=tlg_title,
                                thumb_url=tlg_img_thumb,
                                input_message_content=InputTextMessageContent(tlg_content[:MAX_LENGTH], disable_web_page_preview=(not tlg_preview), parse_mode=pm))
                        )
                    # @design We can add an else clause and go to the normal (json-less) mode below
    else:
        results = [
            InlineQueryResultArticle(
                id=uuid4(),
                # Telegram truncates itself, so this is redundant.
                title=out[:150],
                input_message_content=InputTextMessageContent(out[:MAX_LENGTH], disable_web_page_preview=False))
        ]
        files = list(Path(cwd).glob('*'))
        files.sort()
        for f in files:
            if not f.is_dir():
                file_add = f.absolute()
                base_name = str(os.path.basename(file_add))
                ext = f.suffix
                file = open(file_add, "rb")
                uploaded_file = updater.bot.send_document(tmp_chat, file)
                file.close()
                if uploaded_file.document:
                    # print(f"File ID: {uploaded_file.document.file_id}")
                    results.append(
                        InlineQueryResultCachedDocument(
                            id=uuid4(),
                            title=base_name,
                            document_file_id=uploaded_file.document.file_id)
                    )
                else:
                    print("BUG?: Uploaded file had no document!")

    z("command rm -r {cwd}")
    print(f"len(results): {len(results)}")
    return results


def main():
    # Create the Updater and pass it your bot's token.
    # Make sure to set use_context=True to use the new context based callbacks
    # Post version 12 this will no longer be necessary
    global updater
    updater = Updater(os.environ["TELEGRAM_TOKEN"], use_context=True)

    # Get the dispatcher to register handlers
    dp = updater.dispatcher

    # on different commands - answer in Telegram
    # dp.add_handler(CommandHandler("start", start))
    # dp.add_handler(CommandHandler("help", help_command))

    dp.add_handler(InlineQueryHandler(inlinequery))

    # Start the Bot
    updater.start_polling()
    # updater.start_webhook # convert to this?

    print("Ready!")
    # Block until the user presses Ctrl-C or the process receives SIGINT,
    # SIGTERM or SIGABRT. This should be used most of the time, since
    # start_polling() is non-blocking and will stop the bot gracefully.
    updater.idle()


if __name__ == '__main__':
    main()
