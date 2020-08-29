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
from os import makedirs
from pathlib import Path
from IPython import embed
from brish import z, zp, bsh
from uuid import uuid4
import re
from cachetools import cached, LRUCache, TTLCache
from threading import RLock
import traceback

from telegram import InlineQueryResultArticle, ParseMode, \
    InputTextMessageContent, InlineQueryResultCachedDocument, InlineQueryResultCachedVideo, InlineQueryResultCachedGif, InlineQueryResultCachedMpeg4Gif, InlineQueryResultCachedPhoto, InlineQueryResultCachedVoice
from telegram.ext import Updater, InlineQueryHandler, CommandHandler
from telegram.utils.helpers import escape_markdown

##
# Enable logging
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
                    level=logging.INFO)

logger = logging.getLogger(__name__)
##
PAF = re.compile(r"(?im)^(?:\.a(n?)\s+)?((?:.|\n)*)\s+fin$")
PDI = re.compile(r"(?im)^\.di\s+(\S+)(?:\s+(\S*))?\s+fin$")
WHITESPACE = re.compile(r"^\s*$")
dl_base = os.getcwd() + '/dls/'
tmp_chat = -1001496131468
lock_inline = RLock()
##


def start(update, context):
    """Send a message when the command /start is issued."""
    update.message.reply_text('Hi!')


def help_command(update, context):
    """Send a message when the command /help is issued."""
    update.message.reply_text('Help!')


def isAdmin(update):
    res = False
    try:
        res = update.effective_user.username in ["Arstar"]
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
    if (not isAdmin(update)):
        return
    def ans_text(text: str):
        update.inline_query.answer([InlineQueryResultArticle(
            id=uuid4(),
            title=text,
            input_message_content=InputTextMessageContent(text, disable_web_page_preview=False))], cache_time=1, is_personal=True)

    with lock_inline:
        query = update.inline_query.query
        if query == ".x":
            bsh.restart()
            cache.clear()
            ans_text("Restarted")
            return
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
                    update.inline_query.answer([result], cache_time=1, is_personal=True)
                except:
                    ans_text(traceback.format_exc())
            else:
                ans_text(f"Invalid kind: {c_kind}")
            return
        m = PAF.match(query)
        if m == None:
            return
        command = m.group(2)
        if m.group(1) == 'n':
            # embed()
            command = 'noglob ' + command
        print(f"Inline command accepted: {command}")
        results = get_results(command)
        update.inline_query.answer(results, cache_time=1, is_personal=True)


cache = TTLCache(maxsize=256, ttl=3600)
@cached(cache)
def get_results(command: str):
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
    results = [
        InlineQueryResultArticle(
            id=uuid4(),
            title=out[:150], # Telegram truncates itself, so this is redundant.
            input_message_content=InputTextMessageContent(out, disable_web_page_preview=False))
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
            # print(f"File ID: {uploaded_file.document.file_id}")
            results.append(
                InlineQueryResultCachedDocument(
                    id=uuid4(),
                    title=base_name,
                    document_file_id=uploaded_file.document.file_id)
            )
    z("command rm -r {cwd}")
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
