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

from telegram import InlineQueryResultArticle, ParseMode, \
    InputTextMessageContent, InlineQueryResultCachedDocument
from telegram.ext import Updater, InlineQueryHandler, CommandHandler
from telegram.utils.helpers import escape_markdown

##
# Enable logging
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
                    level=logging.INFO)

logger = logging.getLogger(__name__)
##
PAF = re.compile(r"(?im)^(?:.a(n?)\s+)?((?:.|\n)*)\s+fin$")
WHITESPACE = re.compile(r"^\s*$")
dl_base = os.getcwd() + '/dls/'
tmp_chat = -1001496131468
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
    ##
    # There is pretty much no info except the sender in update. context is also useless. So we can't get the replied-to file.
    ##
    if (not isAdmin(update)):
        return

    query = update.inline_query.query
    if query == "x":
        bsh.restart()
        update.inline_query.answer([InlineQueryResultArticle(
            id=uuid4(),
            title="Restarted",
            input_message_content=InputTextMessageContent("Restarted.", disable_web_page_preview=False))], cache_time=0, is_personal=True)
        return

    m = PAF.match(query)
    if m == None:
        return
    command = m.group(2)
    if m.group(1) == 'n':
        # embed()
        command = 'noglob ' + command
    print(f"Inline command accepted: {command}")
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
            title=out[:50],
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
            results.append(
                InlineQueryResultCachedDocument(
                    id=uuid4(),
                    title=base_name,
                    document_file_id=uploaded_file.document.file_id)
            )
    z("command rm -r {cwd}")

    # The cache helps for music, I think
    update.inline_query.answer(results, cache_time=300, is_personal=True)


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
