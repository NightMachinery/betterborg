from telethon import TelegramClient, events, Button, utils
from telethon.tl import types
import itertools
import os
from pathlib import Path
import uuid
import subprocess
import traceback
from dataclasses import dataclass
from typing import Any, Optional
from uniborg import (
    draft_stream,
    guest_util,
    redis_util,
    shell_settings,
    shell_stream,
    stream_driver,
    tg_format,
    tg_raw,
    topics,
    util,
)
from uniborg.shell_settings import FinalMode
from uniborg.shell_stream import JobState, ShellJob, StopReason
from uniborg.stream_driver import StreamMode
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


async def _brishz_on_shell_pool(*, event, cwd, cmd, fork):
    """`brishz` on the shell pool that is current when the command runs.

    Not the one of when the message came: `.x` can replace the pool while the
    replied-to files still download, and the pool it retires takes no more
    commands.
    """
    await util.brishz(event, cwd, cmd, fork=fork, brish=util.persistent_brish)


##
#: Live output in chats: a running command's output shows in a *preview*,
#: which becomes its final (docs/shell_streaming.md, "Live output in chats").


@dataclass(frozen=True)
class EditPace:
    """How often an edited preview changes: every INTERVAL seconds, then
    every SLOW_INTERVAL seconds once the command has run for `slow_after`."""

    interval: float
    slow_interval: float


@dataclass(frozen=True)
class LiveTiming:
    #: A command that ends sooner shows no preview: only its final, as
    #: before live output.
    preview_delay: float = 2.0
    private: EditPace = EditPace(interval=2.0, slow_interval=5.0)
    #: Slower, to stay within a group's edit budget.
    groups: EditPace = EditPace(interval=4.0, slow_interval=10.0)
    slow_after: float = 30.0


LIVE_TIMING = LiveTiming()
#: A preview's length in UTF-16 units: within one message (4096), with room
#: for a draft's elapsed-time suffix, so `util.edit_message` never splits it.
PREVIEW_UNITS = 4000
PREVIEW_CURSOR = "▌"
#: Telegram's limit for one message, in UTF-16 units.
MESSAGE_UNITS = 4096
LONG_OUTPUT_LINE = "✂️ The full output is in the file below."
FINISHED_TEXT = "Finished; output below."
STOPPED_BEFORE_IT_RAN = "⏹ Stopped before it ran."


@dataclass
class LiveRun:
    """How `_run_live` went."""

    #: None when the job was stopped before its command ran.
    result: Optional[util.CommandResult]
    #: What showed the output while it ran (a message, or a draft stand-in);
    #: None when the command ended within the preview delay.
    preview: Any = None


async def _relay_changes(job, task, *, changed):
    """Sets CHANGED on each change of JOB, until TASK ends or JOB is dropped.

    The one reader of `job.output.changed`, so the preview's pump and this
    watch can both follow it. A job stopped while it waited for a shell is
    *dropped*: its command never runs.
    """
    while not (task.done() or job.dropped):
        waiter = asyncio.ensure_future(job.output.changed.wait())
        try:
            await asyncio.wait({task, waiter}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            waiter.cancel()
        job.output.changed.clear()
        changed.set()


def _log_late_failure(task):
    if not task.cancelled() and task.exception() is not None:
        logger.warning("A dropped shell job failed", exc_info=task.exception())


async def _open_preview_or_none(open_preview):
    try:
        return await open_preview()
    except Exception:
        logger.warning("Could not show a command's output live", exc_info=True)
        return None


async def _run_live(
    job,
    *,
    produce,
    open_preview,
    preview_text,
    pace,
    edit_interval,
    preview_delay,
) -> LiveRun:
    """Runs `produce()` for JOB, showing its output live once PREVIEW_DELAY passes.

    `open_preview()` sends the preview, then `preview_text(preview)` is what
    it shows, at PACE (`stream_driver.follow`), until the command ends. A
    draft preview's Stop button stops the job. A job dropped while it waited
    for a shell returns at once, with no result. A producer's error removes
    the preview and propagates. A cancel stops the command (SHUTDOWN).
    """
    task = asyncio.ensure_future(produce())
    changed = asyncio.Event()
    ended = asyncio.ensure_future(_relay_changes(job, task, changed=changed))
    preview = None
    try:
        await asyncio.wait({ended}, timeout=preview_delay)
        if not ended.done():
            preview = await _open_preview_or_none(open_preview)
        if preview is not None:
            job.preview_id = preview.id
            editor = stream_driver.PacedEditor(
                preview,
                edit_interval=edit_interval,
                parse_mode=None,
                pace=pace,
                report_failures=True,
                logger=logger,
            )
            async with stream_driver.stop_wired(
                preview, on_stop=partial(job.cancel, reason=StopReason.USER)
            ):
                try:
                    await stream_driver.follow(
                        editor,
                        render=partial(preview_text, preview),
                        changed=changed,
                        done=ended,
                    )
                finally:
                    util.forget_edit_chain(preview)
        await ended
    except asyncio.CancelledError:
        job.cancel(reason=StopReason.SHUTDOWN)
        task.cancel()
        ended.cancel()
        raise
    if not task.done():
        #: Dropped: the producer frees its shell once it gets one.
        task.add_done_callback(_log_late_failure)
        return LiveRun(result=None, preview=preview)
    try:
        return LiveRun(result=task.result(), preview=preview)
    except Exception:
        if preview is not None:
            await _remove_preview(preview)
        raise


def _preview_header(job, *, stop_hint) -> str:
    if job.stopped:
        return f"⏹ #{job.id} stopping…"
    match job.state:
        case JobState.QUEUED:
            header = f"⏳ #{job.id} waiting for a free shell"
        case JobState.RUNNING | JobState.ENDED | JobState.DONE:
            header = f"⏳ #{job.id}"
        case JobState.STOPPING:
            raise ValueError("a stopping job has a stop reason")
        case _:
            raise ValueError(f"Unknown job state: {job.state!r}")
    return f"{header} · .k to stop" if stop_hint else header


def _preview_text(job, *, render, stop_hint) -> str:
    """The header, a blank line and the output's tail, within PREVIEW_UNITS.

    STOP_HINT names `.k`, for a preview that shows no Stop button.
    """
    header = _preview_header(job, stop_hint=stop_hint)
    room = (
        PREVIEW_UNITS
        - tg_format.utf16_len(header)
        - 2
        - tg_format.utf16_len(PREVIEW_CURSOR)
    )
    tail = job.output.tail_text(max_units=room, render=render)
    return f"{header}\n\n{tail}"


def _shows_stop_button(preview) -> bool:
    return isinstance(preview, draft_stream.DraftAnswerMessage)


async def _send_preview(event, text):
    return await event.respond(
        text,
        reply_to=event.message,
        parse_mode=None,
        link_preview=False,
        silent=True,
    )


def _wants_drafts(event, prefs) -> bool:
    """Whether EVENT's preview should be a draft, as PREFS set for its chat.

    Only a bot can show one; where Telegram refuses (groups), the preview
    falls back to an edited message.
    """
    return bool(
        borg.me.bot
        and draft_stream.DRAFTS_SUPPORTED
        and stream_driver.stream_mode(prefs, scope=stream_driver.stream_scope(event))
        == StreamMode.DRAFTS
    )


async def _open_chat_preview(event, *, job, drafts, render):
    text = partial(_preview_text, job, render=render)
    return await stream_driver.open_stream_target(
        event,
        client=borg,
        drafts=drafts,
        placeholder_text=text(stop_hint=True) + PREVIEW_CURSOR,
        draft_text=text(stop_hint=False) + PREVIEW_CURSOR,
        send_placeholder=_send_preview,
        top_msg_id=topics.private_topic_id(event.message) if drafts else None,
        parse_mode=None,
        logger=logger if drafts else None,
    )


def _stop_note(job, *, retcode) -> str:
    match job.stop_reason:
        case None:
            return ""
        case StopReason.USER:
            return f"⏹ Stopped (exit {retcode})."
        case StopReason.SHUTDOWN:
            return f"⏹ Stopped: julia is restarting (exit {retcode})."
        case _:
            raise ValueError(f"Unknown stop reason: {job.stop_reason!r}")


def _final_text(job, result) -> str:
    """What `send_output` would send for RESULT, plus a note when JOB was stopped."""
    if result is None:
        return STOPPED_BEFORE_IT_RAN
    text = util.shell_output_text(result.output, retcode=result.retcode)
    note = _stop_note(job, retcode=result.retcode)
    return f"{text}\n\n{note}" if note else text


async def _remove_preview(preview):
    """Deletes PREVIEW; one that cannot be deleted says it finished instead."""
    try:
        await preview.delete()
    except Exception:
        logger.info("Could not delete a preview", exc_info=True)
        try:
            await stream_driver.show_final(preview, FINISHED_TEXT)
        except Exception:
            logger.warning("Could not mark a preview finished", exc_info=True)


def _long_output_text(text) -> str:
    """The end of TEXT that fits one message, under a line naming the file."""
    room = MESSAGE_UNITS - tg_format.utf16_len(LONG_OUTPUT_LINE) - 1
    return f"{LONG_OUTPUT_LINE}\n{tg_format.tail_utf16(text, room)}"


async def _final_in_preview(event, text, *, preview):
    """FinalMode.EDIT_PREVIEW: the preview becomes TEXT, or its end and a file."""
    as_file = util.discreet_sends_file(text)
    try:
        shown = await stream_driver.show_final(
            preview, _long_output_text(text) if as_file else text.strip()
        )
    except Exception:
        logger.warning("Could not show the final in its preview", exc_info=True)
        await util.discreet_send(event, text, event.message)
        await _remove_preview(preview)
        return
    if as_file:
        await util.discreet_send(event, text, shown)


async def _final_as_reply(event, text, *, preview):
    """FinalMode.NEW_REPLY: TEXT as `send_output` sends it; the preview goes."""
    if isinstance(preview, draft_stream.DraftAnswerMessage):
        #: Its stream has ended. A text reply that starts as the last draft
        #: does adopts it, so it does not linger beside the reply.
        if not util.discreet_sends_file(text):
            await preview.sync_draft(text.strip())
        await util.discreet_send(event, text, event.message)
        return
    await util.discreet_send(event, text, event.message)
    await _remove_preview(preview)


async def _deliver_final(event, text, *, preview, mode):
    if preview is None:
        await util.discreet_send(event, text, event.message)
        return
    match mode:
        case FinalMode.EDIT_PREVIEW:
            await _final_in_preview(event, text, preview=preview)
        case FinalMode.NEW_REPLY:
            await _final_as_reply(event, text, preview=preview)
        case _:
            raise ValueError(f"Unknown final mode: {mode!r}")


async def _run_in_chat(*, cwd, event, request, job, prefs):
    """`run_and_get`'s work for a live `.a`, `.af` or `.aa`: run, show, deliver."""
    if request.brish_mode:
        produce = partial(
            util.brishz_capture,
            cwd=cwd,
            cmd=request.command,
            fork=request.fork,
            job=job,
            brish=util.persistent_brish,
        )
    else:
        produce = partial(
            util.simple_run_capture, cwd=cwd, command=request.command, job=job
        )
    timing = LIVE_TIMING
    edit_pace = timing.private if event.is_private else timing.groups
    live = await _run_live(
        job,
        produce=produce,
        open_preview=partial(
            _open_chat_preview,
            event,
            job=job,
            drafts=_wants_drafts(event, prefs),
            render=False,
        ),
        preview_text=lambda preview: _preview_text(
            job, render=False, stop_hint=not _shows_stop_button(preview)
        ),
        pace=stream_driver.tiered_pace(
            slow_after=timing.slow_after,
            slow_interval=edit_pace.slow_interval,
            cursor=PREVIEW_CURSOR,
        ),
        edit_interval=edit_pace.interval,
        preview_delay=timing.preview_delay,
    )
    text = _final_text(job, live.result)
    await _deliver_final(event, text, preview=live.preview, mode=prefs.final_mode)


def _job_of(event, request):
    return ShellJob(
        owner_id=event.sender_id,
        chat_id=event.chat_id,
        command=request.command,
        message_id=event.message.id,
    )


@borg.on(events.NewMessage(pattern=pattern_a))
async def _(event):
    if guest_util.is_guest_answer(event.message):
        return
    if not (await util.isAdmin(event) and event.message.forward == None):
        return

    request = await parse_shell_request(event.pattern_match)
    if not shell_settings.SHELL_STREAMING:
        if request.brish_mode:
            to_await = partial(
                _brishz_on_shell_pool, cmd=request.command, fork=request.fork
            )
        else:
            to_await = partial(util.simple_run, command=request.command, shell=True)
        await util.run_and_upload(
            event=event, to_await=to_await, album_mode=request.album_mode
        )
        return

    prefs = shell_settings.SETTINGS.get(event.sender_id)
    if request.brish_mode and not util.BRISH_POPEN:
        #: This brish can neither stream a command nor stop one.
        await util.run_and_upload(
            event=event,
            to_await=partial(
                _brishz_on_shell_pool, cmd=request.command, fork=request.fork
            ),
            album_mode=request.album_mode,
        )
        return
    #: The job lasts until the files are sent, so a `.k` meanwhile finds it.
    job = shell_stream.register(_job_of(event, request))
    try:
        await util.run_and_upload(
            event=event,
            to_await=partial(_run_in_chat, request=request, job=job, prefs=prefs),
            album_mode=request.album_mode,
        )
    finally:
        shell_stream.finish(job)


if borg.me.bot:
    stream_driver.register_draft_stop(borg, module=__name__)


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
GUEST_CAPTION_LIMIT = guest_util.CAPTION_LIMIT_UNITS
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

    The text must start with the bot's mention, then whitespace, then `.a`,
    and must not start inside a code block: relayed text (command output, an
    LLM answer) is what a looser rule would let through.
    """
    text = query.text
    after = guest_util.shell_command_after_mention(text, username=_guest_username())
    if after is None:
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


def _free_output_path(cwd):
    """Where the whole output goes in CWD, never over a file the command made."""
    path = Path(cwd, GUEST_OUTPUT_FILE)
    if not path.exists():
        return path
    return path.with_name(f"{path.stem}-{uuid.uuid4().hex[:8]}{path.suffix}")


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
                cwd=cwd,
                cmd=request.command,
                fork=request.fork,
                brish=util.persistent_brish,
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
            _free_output_path(cwd).write_text(output)
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
        note = guest_util.album_note(query)
        if note:
            footer_lines.append(note)

        #: A caption holds far less than a text answer; attaching must not cut
        #: output that the text answer would show whole.
        caption_fits = (
            tg_format.utf16_len(
                _plain_answer(output, footer_lines=footer_lines, limit=10**9)
            )
            <= GUEST_CAPTION_LIMIT
        )
        if (
            len(sent) == 1
            and caption_fits
            and await _finalize_with_attachment(
                answer, sent[0], output=output, footer_lines=footer_lines
            )
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
