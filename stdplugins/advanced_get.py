from telethon import TelegramClient, events, Button, utils
from telethon.tl import types
import itertools
import os
from pathlib import Path
import uuid
import subprocess
import traceback
from dataclasses import dataclass
from uniborg import guest_util, redis_util, tg_format, tg_raw, util
from uniborg.util import clean_cmd, embed2, brishz
from IPython import embed
import re
import asyncio
from brish import z, zp, Brish
from functools import partial

pattern_a = re.compile(
    r"(?im)^(?:\[(?:(?:In reply to)|(?:Forwarded from))\s+[^]]*\]\n)*\.a(?P<nobrish>a)?(?P<fork>f)?(?P<noalbum>d)?(?P<noglob>n)?\s+(?P<cmd>(?:.|\n)*)$"
)


@dataclass
class ShellRequest:
    command: str
    brish_mode: bool
    fork: bool
    album_mode: bool


async def parse_shell_request(match) -> ShellRequest:
    """Reads a `pattern_a` match: `.a[a][f][d][n] CMD`."""
    command = await clean_cmd(match.group("cmd"))
    if match.group("noglob") == "n":
        command = "noglob " + command
    return ShellRequest(
        command=command,
        brish_mode=not bool(match.group("nobrish")),
        fork=match.group("fork") != "f",
        album_mode=not bool(match.group("noalbum")),
    )


@borg.on(events.NewMessage(pattern=pattern_a))
async def _(event):
    if guest_util.is_guest_answer(event.message):
        return
    if not (await util.isAdmin(event) and event.message.forward == None):
        return

    request = await parse_shell_request(event.pattern_match)
    if request.brish_mode:
        to_await = partial(brishz, cmd=request.command, fork=request.fork)
    else:
        to_await = partial(util.simple_run, command=request.command, shell=True)
    await util.run_and_upload(
        event=event, to_await=to_await, album_mode=request.album_mode
    )


@borg.on(util.admin_cmd(pattern="^\.xf$"))
async def _(event):
    util.init_brishes()
    await event.reply(
        "Reinitialized brishes. Note that old running instances can still rejoin."
    )


@borg.on(util.admin_cmd(pattern="^\.(x|sbb)$"))
async def _(event):
    util.restart_brishes()
    await event.reply("Restarted brishes.")


##
#: Guest mode: `@thisbot .a CMD` in any chat the bot is not in (see
#: docs/guest_mode.md). Only an admin's explicit, leading mention runs anything.

GUEST_TITLE = "Shell"
#: A late command should not run; Telegram also rejects late answers.
GUEST_MAX_AGE_SECONDS = 60
GUEST_TEXT_LIMIT = 4096
GUEST_CAPTION_LIMIT = 1024
#: Room left for the footer after the output block.
GUEST_FOOTER_RESERVE = 300
GUEST_OUTPUT_FILE = "output.txt"

_guest_claims = guest_util.QueryClaims(
    backend=guest_util.redis_claim_backend(redis_util.get_redis)
)


def _guest_username() -> str:
    return borg.me.username


def _guest_shell_match(query):
    """The `pattern_a` match of a strict guest trigger, or None.

    The text must start with the bot's mention followed directly by `.a`, and
    must not start inside a code block: relayed text (command output, an LLM
    answer) is what a looser rule would let through.
    """
    text = query.text
    after = guest_util.text_after_leading_mention(text, username=_guest_username())
    if after is None or not after.startswith(".a"):
        return None
    mention_at = tg_format.utf16_len(text[: len(text) - len(text.lstrip())])
    for entity in query.trigger.entities or []:
        if isinstance(entity, (types.MessageEntityPre, types.MessageEntityCode)) and (
            entity.offset <= mention_at < entity.offset + entity.length
        ):
            return None
    return pattern_a.match(after)


async def _answer_note(query, text):
    await guest_util.answer_note(borg, query, text, title=GUEST_TITLE, logger=logger)


def _plain_answer(output, *, footer_lines, limit=GUEST_TEXT_LIMIT):
    """The answer text: `output` as plain text, like `.a`, then the footer lines."""
    footer = "\n".join(footer_lines)
    budget = limit - (tg_format.utf16_len(footer) + 2 if footer else 0)
    body = tg_format.truncate_utf16(output, budget)
    return f"{body}\n\n{footer}" if footer else body


async def _send_files_to_dm(caller_id, *, request, files):
    """Sends the command's files to the caller's own DM with the bot.

    Never to the guest chat: in a private guest chat its id is the other
    participant. Returns (sent messages, error text or None).
    """
    failures = []

    async def on_error():
        failures.append(traceback.format_exc())
        logger.warning("Could not send a guest shell file:\n%s", failures[-1])

    try:
        header = await borg.send_message(
            caller_id,
            f"📎 Files from a guest command:\n{request.command[:200]}",
            link_preview=False,
        )
    except Exception:
        logger.warning("Could not DM the guest shell caller", exc_info=True)
        return [], "⚠️ Could not send the files to your DM; start me there first."

    sent = await util.upload_output_files(
        caller_id,
        files,
        album_mode=request.album_mode,
        reply_to=header.id,
        on_error=on_error,
    )
    error = f"⚠️ {len(failures)} file(s) failed to send." if failures else None
    return sent, error


async def _finalize_with_attachment(answer, dm_message, *, output, footer_lines):
    """Shows the one file sent to the DM in the guest answer too.

    An inline edit cannot upload, so this reuses the DM copy, with the output
    as a caption. Returns False when that fails; the caller then edits in text.
    """
    try:
        media = utils.get_input_media(dm_message.media)
        caption = _plain_answer(
            output, footer_lines=footer_lines, limit=GUEST_CAPTION_LIMIT
        )
        await answer.finalize(text=caption, media=media)
        return True
    except Exception:
        logger.warning("Could not attach the file to the guest answer", exc_info=True)
        return False


async def _run_guest_shell(query, request, answer):
    results = []

    async def to_await(*, cwd, event):
        if request.brish_mode:
            result = await util.brishz_capture(
                cwd=cwd, cmd=request.command, fork=request.fork
            )
        else:
            result = await util.simple_run_capture(cwd=cwd, command=request.command)
        results.append(result)

    cwd = f"{util.dl_base}{uuid.uuid4()}/"
    try:
        await util.run_and_get(None, to_await, cwd, messages=query.messages)
        (result,) = results
        output = result.output.strip() or f"The process exited {result.retcode}."
        truncated = (
            tg_format.utf16_len(output) > GUEST_TEXT_LIMIT - GUEST_FOOTER_RESERVE
        )
        if truncated:
            Path(cwd, GUEST_OUTPUT_FILE).write_text(output)
        files = sorted(p for p in Path(cwd).glob("*") if not p.is_dir())

        footer_lines = []
        sent = []
        if result.retcode != 0:
            footer_lines.append(f"exit {result.retcode}")
        if files:
            sent, error = await _send_files_to_dm(
                query.caller_id, request=request, files=files
            )
            if sent:
                footer_lines.append(f"📎 {len(sent)} file(s) sent to your DM")
            if error:
                footer_lines.append(error)
        if truncated:
            footer_lines.append("✂️ Output truncated; the full output is a file.")

        if len(sent) == 1 and await _finalize_with_attachment(
            answer, sent[0], output=output, footer_lines=footer_lines
        ):
            return
        await answer.finalize(text=_plain_answer(output, footer_lines=footer_lines))
    finally:
        await util.remove_potential_file(cwd)


async def guest_shell(query):
    if not guest_util.mentions(query.text, username=_guest_username()):
        #: A reply to one of our answers, without a mention: not a call.
        return
    if not util.is_admin_by_id(query.caller_id):
        await _answer_note(query, "Not available here.")
        return
    match = _guest_shell_match(query)
    if match is None:
        await _answer_note(
            query, f"Usage: @{_guest_username()} .a COMMAND (the mention first)"
        )
        return

    request = await parse_shell_request(match)
    try:
        inline_id = await tg_raw.answer_guest(
            borg, query_id=query.query_id, title=GUEST_TITLE, text="⏳ Running…"
        )
    except Exception:
        #: No answer, no execution: the command must not run unseen, or twice.
        logger.exception("Could not answer guest query %s", query.query_id)
        return

    logger.info("Guest shell: running a command for %s", query.caller_id)
    async with tg_raw.InlineEditor(borg, inline_id) as editor:
        answer = guest_util.GuestAnswerMessage(editor, logger=logger)
        try:
            await _run_guest_shell(query, request, answer)
        except Exception:
            await answer.finalize(
                text=_plain_answer(
                    "Julia encountered an exception. :(\n" + traceback.format_exc(),
                    footer_lines=[],
                )
            )


guest_util.register_guest_handler(
    borg,
    guest_shell,
    claims=_guest_claims,
    max_age_seconds=GUEST_MAX_AGE_SECONDS,
    logger=logger,
)
