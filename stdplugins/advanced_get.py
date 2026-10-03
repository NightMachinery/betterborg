from telethon import TelegramClient, events, Button, utils
from telethon.tl import types
import itertools
import os
from pathlib import Path
import uuid
import subprocess
import time
import traceback
from dataclasses import dataclass
from typing import Any, Callable, Optional
from telethon import errors
from uniborg import (
    bot_util,
    callback_util,
    draft_stream,
    guest_util,
    redis_util,
    shell_settings,
    shell_stream,
    stream_driver,
    term_render,
    tg_compat,
    tg_format,
    tg_raw,
    topics,
    util,
)
from uniborg.shell_settings import FinalMode, ShellPrefs
from uniborg.shell_stream import CancelOutcome, JobState, ShellJob, StopReason
from uniborg.stream_driver import STREAM_MODE_NAMES, STREAM_SCOPE_NAMES, StreamMode
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
#: Telegram's limit for one message, in UTF-16 units.
MESSAGE_UNITS = 4096
#: A preview's length in UTF-16 units. `util.edit_message` splits a longer
#: text that has a newline near its end into two messages, even within
#: MESSAGE_UNITS; this one it never splits. It also leaves room for a draft's
#: elapsed-time suffix.
PREVIEW_UNITS = MESSAGE_UNITS - util.SPLIT_SEARCH_CHARS
PREVIEW_CURSOR = "▌"
#: Output of up to this many bytes is rendered on the event loop, and more
#: in a thread: rendering costs up to about 0.35 s per MiB
#: (docs/shell_streaming.md), so this holds the loop for at most about 20 ms.
RENDER_ON_LOOP_BYTES = 64 * 2**10
#: An edited preview's Stop button sends this, then the job's id.
STOP_CALLBACK_PREFIX = "shk:"
STOP_BUTTON_TEXT = "⏹ Stop"
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
    """Whether PREVIEW has a Stop button.

    A draft has its own on a Telethon that can build one (1.45;
    `draft_stream.STOP_SUPPORTED`). An edited message has ours on a bot
    (`_stop_buttons`); a userbot cannot send inline buttons.
    """
    if isinstance(preview, draft_stream.DraftAnswerMessage):
        return draft_stream.STOP_SUPPORTED
    return bool(borg.me.bot)


def _stop_buttons(job) -> list:
    """An edited preview's Stop button. Edits keep it: `Message.edit` reuses
    the message's reply markup unless told otherwise, and only the final's
    edit (`stream_driver.show_final`) removes it."""
    return [
        [
            tg_compat.callback_button(
                STOP_BUTTON_TEXT, data=f"{STOP_CALLBACK_PREFIX}{job.id}"
            )
        ]
    ]


async def _send_preview(event, text, *, buttons=None):
    return await event.respond(
        text,
        reply_to=event.message,
        parse_mode=None,
        link_preview=False,
        silent=True,
        buttons=buttons,
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
    buttons = _stop_buttons(job) if borg.me.bot else None
    return await stream_driver.open_stream_target(
        event,
        client=borg,
        drafts=drafts,
        placeholder_text=text(stop_hint=buttons is None) + PREVIEW_CURSOR,
        draft_text=text(stop_hint=not draft_stream.STOP_SUPPORTED) + PREVIEW_CURSOR,
        send_placeholder=partial(_send_preview, buttons=buttons),
        top_msg_id=topics.private_topic_id(event.message) if drafts else None,
        parse_mode=None,
        logger=logger if drafts else None,
    )


async def _off_the_loop_if_large(read, *, size):
    """`read()`, in a thread when SIZE is over RENDER_ON_LOOP_BYTES."""
    if size <= RENDER_ON_LOOP_BYTES:
        return read()
    return await asyncio.get_running_loop().run_in_executor(None, read)


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


async def _final_text(job, result, *, render) -> str:
    """What `send_output` would send for RESULT, plus a note when JOB was stopped.

    With RENDER, the output is shown as a terminal would (`term_render`);
    without, it is RESULT's own, as before.
    """
    if result is None:
        return STOPPED_BEFORE_IT_RAN
    output = result.output
    if render:
        output = await _off_the_loop_if_large(
            partial(job.output.final_text, render=True), size=job.output.written
        )
    text = util.shell_output_text(output, retcode=result.retcode)
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
        #: The pool of now: `.x` during the download retired the earlier one.
        job.pool = util.persistent_brish
        produce = partial(
            util.brishz_capture,
            cwd=cwd,
            cmd=request.command,
            fork=request.fork,
            job=job,
            brish=job.pool,
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
            render=prefs.render,
        ),
        preview_text=lambda preview: _preview_text(
            job, render=prefs.render, stop_hint=not _shows_stop_button(preview)
        ),
        pace=stream_driver.tiered_pace(
            slow_after=timing.slow_after,
            slow_interval=edit_pace.slow_interval,
            cursor=PREVIEW_CURSOR,
        ),
        edit_interval=edit_pace.interval,
        preview_delay=timing.preview_delay,
    )
    text = await _final_text(job, live.result, render=prefs.render)
    await _deliver_final(event, text, preview=live.preview, mode=prefs.final_mode)


async def _run_unstoppable(*, cwd, event, request, prefs):
    """`.a` on a brish without `popen`: as before live output, rendered."""
    result = await util.brishz_capture(
        cwd=cwd,
        cmd=request.command,
        fork=request.fork,
        brish=util.persistent_brish,
    )
    output = result.output
    if prefs.render:
        output = await _off_the_loop_if_large(
            partial(term_render.render, output), size=len(output)
        )
    await util.send_output(event, output, retcode=result.retcode)


def _job_of(event, request):
    return ShellJob(
        owner_id=event.sender_id,
        chat_id=event.chat_id,
        command=request.command,
        message_id=event.message.id,
    )


async def _is_admin_command(event) -> bool:
    """The gate of `.a`: an admin's message, not forwarded, and not one of
    our own guest answers echoed back (its text may be the caller's doing)."""
    if guest_util.is_guest_answer(event.message):
        return False
    return bool(await util.isAdmin(event) and event.message.forward is None)


@borg.on(events.NewMessage(pattern=pattern_a))
async def _(event):
    if not await _is_admin_command(event):
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
            to_await=partial(_run_unstoppable, request=request, prefs=prefs),
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


##
#: `.k`: stops a running command (a job), as a reply to it or its preview, or
#: by number. Visible jobs are `shell_stream.visible`'s: the jobs of this chat,
#: and in an admin's private chat with the bot also their guest jobs.

pattern_k = re.compile(r"(?i)^\.k(?:\s+(?P<arg>\S+))?\s*$")
NO_RUNNING_JOB = "No running command here."
NOT_A_JOB = "That message has no running command."
OLD_BRISH_NOTE = ".a cannot be stopped until brish is upgraded; .aa can."
STREAMING_OFF_NOTE = (
    "Live output is off (borg_shell_streaming=0), so no command can be stopped."
)
KILL_USAGE = (
    "Usage: .k as a reply to a command or its preview, or alone; "
    ".k N stops #N, .k all stops every one here, .k ls lists them."
)
#: How much of a command a list shows.
COMMAND_PREVIEW_CHARS = 60
#: A toast for a press by anyone but an admin.
ADMINS_ONLY = "Only the bot's admins can do that."
OUTDATED_BUTTON = "That button is out of date."


def _is_running(job) -> bool:
    """Whether JOB's command still waits, runs or is being stopped."""
    match job.state:
        case JobState.QUEUED | JobState.RUNNING | JobState.STOPPING:
            return True
        case JobState.ENDED | JobState.DONE:
            return False
        case _:
            raise ValueError(f"Unknown job state: {job.state!r}")


def stop_text(job, outcome) -> str:
    """What a stop of JOB that gave OUTCOME says, as a reply or a toast."""
    match outcome:
        case CancelOutcome.STOPPING | CancelOutcome.NOT_STARTED:
            return f"⏹ Stopping #{job.id}…"
        case CancelOutcome.ALREADY_STOPPING:
            return f"#{job.id} is already stopping."
        case CancelOutcome.FINISHED:
            return f"#{job.id} has already ended."
        case _:
            raise ValueError(f"Unknown cancel outcome: {outcome!r}")


def _stop(job) -> str:
    return stop_text(job, job.cancel(reason=StopReason.USER))


def _age_text(seconds) -> str:
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    minutes, seconds = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m {seconds:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes:02d}m"


def _job_line(job, *, now) -> str:
    command = " ".join(job.command.split())
    if len(command) > COMMAND_PREVIEW_CHARS:
        command = command[:COMMAND_PREVIEW_CHARS] + "…"
    match job.state:
        case JobState.QUEUED:
            note = " · waiting"
        case JobState.STOPPING:
            note = " · stopping"
        case JobState.RUNNING | JobState.ENDED | JobState.DONE:
            note = ""
        case _:
            raise ValueError(f"Unknown job state: {job.state!r}")
    return f"#{job.id} · {_age_text(now - job.started_at)}{note} · {command}"


def job_list_text(jobs, *, now) -> str:
    """The running JOBS, a line each; NOW is `time.monotonic()`."""
    lines = [_job_line(job, now=now) for job in jobs]
    return "\n".join(
        ["Running here:", *lines, "", ".k N stops one; .k all stops them all."]
    )


def _nothing_text(text) -> str:
    """TEXT, and why a command may be running that `.k` cannot see."""
    if not shell_settings.SHELL_STREAMING:
        return f"{text}\n{STREAMING_OFF_NOTE}"
    if not util.BRISH_POPEN:
        return f"{text}\n{OLD_BRISH_NOTE}"
    return text


async def kill_reply(event, *, clock=time.monotonic) -> str:
    """Stops what `.k` in EVENT names, and says what it did."""
    arg = (event.pattern_match.group("arg") or "").lower()
    jobs = [
        job
        for job in shell_stream.visible(
            chat_id=event.chat_id, caller_id=event.sender_id
        )
        if _is_running(job)
    ]
    if not arg:
        #: In a topic every message has a reply header; only a real reply
        #: names a job.
        target = await topics.resolve_reply_target(event.message)
        if target is not None:
            job = shell_stream.find(chat_id=event.chat_id, message_id=target.msg_id)
            return _stop(job) if job is not None else _nothing_text(NOT_A_JOB)
        if not jobs:
            return _nothing_text(NO_RUNNING_JOB)
        if len(jobs) == 1:
            return _stop(jobs[0])
        return job_list_text(jobs, now=clock())
    if arg == "ls":
        return (
            job_list_text(jobs, now=clock()) if jobs else _nothing_text(NO_RUNNING_JOB)
        )
    if arg == "all":
        return "\n".join(map(_stop, jobs)) if jobs else _nothing_text(NO_RUNNING_JOB)
    number = re.fullmatch(r"#?(\d+)", arg)
    if number is None:
        return KILL_USAGE
    job_id = int(number.group(1))
    for job in jobs:
        if job.id == job_id:
            return _stop(job)
    return _nothing_text(f"No running command #{job_id} here.")


@borg.on(events.NewMessage(pattern=pattern_k))
async def kill_handler(event):
    if not await _is_admin_command(event):
        return
    await event.reply(await kill_reply(event), parse_mode=None, link_preview=False)


@callback_util.hold_bare_answers
async def stop_press_handler(event):
    """A press of an edited preview's Stop button: stops its job, with a toast.

    Only admins (`.a`'s gate) may stop; anyone else gets a toast that says
    so. The job must be the one whose preview was pressed. The preview's
    header then shows the stop, at the preview's pace.
    """
    if not await util.isAdmin(event):
        await event.answer(ADMINS_ONLY)
        return
    data = event.data.decode("utf-8", "replace")
    job_id = data.removeprefix(STOP_CALLBACK_PREFIX)
    if not job_id.isdigit():
        await event.answer(OUTDATED_BUTTON)
        return
    job = shell_stream.JOBS.get(int(job_id))
    #: Job ids start again at 1 after a restart, so a button left from before
    #: one names a newer job; only the job's own preview may stop it.
    if (
        job is None
        or job.chat_id != event.chat_id
        or job.preview_id != event.message_id
    ):
        await event.answer(f"#{job_id} has already ended.")
        return
    await event.answer(_stop(job))


if borg.me.bot:
    stream_driver.register_draft_stop(borg, module=__name__)
    borg.on(events.CallbackQuery(pattern=re.escape(STOP_CALLBACK_PREFIX).encode()))(
        stop_press_handler
    )


def old_pool_note(old_pool) -> str:
    """A line on the jobs still running on OLD_POOL, the retired shell pool.

    A restart does not stop them: they keep their pool until they end.
    """
    count = sum(
        1
        for job in shell_stream.JOBS.values()
        if old_pool is not None and job.pool is old_pool and _is_running(job)
    )
    if count == 0:
        return ""
    if count == 1:
        return "\n1 command still runs on the old pool; .k stops it."
    return f"\n{count} commands still run on the old pool; .k stops them."


@borg.on(util.admin_cmd(pattern="^\.xf$"))
async def reinit_brishes_handler(event):
    old_pool = util.persistent_brish
    util.init_brishes()
    await event.reply(
        "Reinitialized brishes. Note that old running instances can still rejoin."
        + old_pool_note(old_pool)
    )


@borg.on(util.admin_cmd(pattern="^\.(x|sbb)$"))
async def restart_brishes_handler(event):
    old_pool = util.persistent_brish
    util.restart_brishes()
    await event.reply("Restarted brishes." + old_pool_note(old_pool))


##
#: `/settings`: each admin's shell settings (`shell_settings.ShellPrefs`), as a
#: panel of buttons in the private chat with the bot, or as text arguments.
#: Only on a bot: a userbot's own `/settings` would also answer one typed to
#: another bot.

BOT_COMMANDS = [
    {
        "command": "settings",
        "description": "Shell settings: live output, finished commands, renderer",
    },
    {"command": "help", "description": "The shell's commands"},
]
SETTINGS_CALLBACK_PREFIX = "shs:"
FINAL_MODE_NAMES = {
    FinalMode.EDIT_PREVIEW: "Edit the preview",
    FinalMode.NEW_REPLY: "New reply",
}
#: The words of `/settings final WORD`, and of its buttons' data.
FINAL_MODE_WORDS = {"edit": FinalMode.EDIT_PREVIEW, "reply": FinalMode.NEW_REPLY}
RENDER_NAMES = {True: "On", False: "Off"}
RENDER_WORDS = {"on": True, "off": False}
SETTINGS_USAGE = (
    "Usage: `/settings`, or `/settings private drafts|edits`, "
    "`/settings groups drafts|edits`, `/settings final edit|reply`, "
    "`/settings render on|off`."
)
SETTINGS_IN_PRIVATE = (
    "The shell settings are in my private chat: send /settings to me there."
)
SETTINGS_NOT_SAVED = "Could not save that setting; try again."
HELP_TEXT = """**Shell**
`.a CMD` runs CMD in a zsh of the shell pool and replies with its output, then the files it leaves. Flags, in this order:
• `.aa`: a new `zsh -c`, outside the pool;
• `.af`: no fork, so what it sets stays for the next `.af`;
• `.ad`: each file on its own, not in albums;
• `.an`: `noglob` before CMD.
A command still running after 2 s shows its output live.
`.k` stops a running command: as a reply to it or its preview, or alone. `.k N` stops #N, `.k all` stops every one here, `.k ls` lists them.
`.x` (or `.sbb`, `.xf`) restarts the shell pool.
/settings: how live output shows, what it becomes when the command ends, and the renderer.
`@{username} .a CMD` runs CMD from any chat, where guest mode is on."""


@dataclass(frozen=True)
class SettingChange:
    """One setting's new value, from `/settings ARGS` or a button of its panel."""

    apply: Callable[[ShellPrefs], None]
    #: What it sets, as the press's toast says it: "Groups: Drafts."
    summary: str


def _set_field(prefs, *, name, value):
    setattr(prefs, name, value)


def setting_change(words: list[str]) -> Optional[SettingChange]:
    """The change WORDS name (`["final", "reply"]`, any case), or None.

    A button's data is its prefix, then the same words joined by ":".
    """
    words = [word.lower() for word in words]
    choice = stream_driver.stream_choice_of_args(words)
    if choice is not None:
        return SettingChange(
            apply=partial(
                stream_driver.set_stream_mode, scope=choice.scope, mode=choice.mode
            ),
            summary=f"{STREAM_SCOPE_NAMES[choice.scope]}: "
            f"{STREAM_MODE_NAMES[choice.mode]}.",
        )
    match words:
        case ["final", word] if word in FINAL_MODE_WORDS:
            mode = FINAL_MODE_WORDS[word]
            return SettingChange(
                apply=partial(_set_field, name="final_mode", value=mode),
                summary=f"When it ends: {FINAL_MODE_NAMES[mode]}.",
            )
        case ["render", word] if word in RENDER_WORDS:
            render = RENDER_WORDS[word]
            return SettingChange(
                apply=partial(_set_field, name="render", value=render),
                summary=f"Renderer: {RENDER_NAMES[render].lower()}.",
            )
        case _:
            return None


def _settings_text(prefs) -> str:
    """The panel: what each setting does and costs, and its value, in Markdown."""
    if draft_stream.STOP_SUPPORTED:
        drafts = (
            "Telegram's live draft, with a Stop button. While a bot's draft is "
            "live, Telegram for Android disables the send button, so a long "
            "command keeps you from typing: press Stop, or choose Edits."
        )
    else:
        drafts = (
            "Telegram's live draft. This bot's drafts have no Stop button, and "
            "while a bot's draft is live, Telegram for Android disables the send "
            "button, so a long command keeps you from typing, even `.k`: choose "
            "Edits if that matters."
        )
    lines = [
        "**Shell settings**",
        "",
        "**Live output**: a command still running after 2 s shows its output "
        "in a preview.",
        f"• **Drafts**: {drafts} Telegram allows drafts only in private chats; "
        "elsewhere they fall back to edits.",
        "• **Edits**: a message edited as the output grows, with a Stop button.",
        *stream_driver.stream_mode_lines(prefs),
        "",
        "**When a command with a preview ends**:",
        "• **Edit the preview**: the preview becomes the output. An edited "
        "message changes silently, with no notification; a draft becomes a new "
        "message, which notifies.",
        "• **New reply**: the output arrives as a new reply, which notifies, and "
        "the preview goes.",
        f"Now: **{FINAL_MODE_NAMES[prefs.final_mode]}**",
        "",
        "**Renderer**: show output as a terminal would, so a progress bar shows "
        "its last state and colours are removed. Off shows the output raw.",
        f"Now: **{RENDER_NAMES[prefs.render]}**",
    ]
    if not draft_stream.DRAFTS_SUPPORTED:
        lines.append(
            "\nThis bot's Telethon cannot send drafts, so every preview is an "
            "edited message."
        )
    lines += ["", SETTINGS_USAGE]
    return "\n".join(lines)


def _choice_row(key, words, *, current, names, label) -> list:
    return [
        tg_compat.callback_button(
            f"{'✅ ' if value == current else ''}{label}: {names[value]}",
            data=f"{SETTINGS_CALLBACK_PREFIX}{key}:{word}",
        )
        for word, value in words.items()
    ]


def _settings_rows(prefs) -> list:
    return [
        *stream_driver.stream_mode_rows(
            prefs, callback_prefix=SETTINGS_CALLBACK_PREFIX
        ),
        _choice_row(
            "final",
            FINAL_MODE_WORDS,
            current=prefs.final_mode,
            names=FINAL_MODE_NAMES,
            label="When it ends",
        ),
        _choice_row(
            "render",
            RENDER_WORDS,
            current=prefs.render,
            names=RENDER_NAMES,
            label="Renderer",
        ),
    ]


def _save_change(user_id, change) -> bool:
    prefs = shell_settings.SETTINGS.get(user_id)
    change.apply(prefs)
    return shell_settings.SETTINGS.set(user_id, prefs)


async def settings_handler(event):
    """/settings: the panel, or with arguments a change, then the panel."""
    if not await _is_admin_command(event):
        return
    if not event.is_private:
        await event.reply(SETTINGS_IN_PRIVATE, parse_mode=None)
        return
    args = (event.pattern_match.group("args") or "").split()
    if args:
        change = setting_change(args)
        if change is None:
            await event.reply(SETTINGS_USAGE, parse_mode="md")
            return
        if not _save_change(event.sender_id, change):
            await event.reply(SETTINGS_NOT_SAVED, parse_mode=None)
    prefs = shell_settings.SETTINGS.get(event.sender_id)
    await event.reply(
        _settings_text(prefs),
        parse_mode="md",
        link_preview=False,
        buttons=_settings_rows(prefs),
    )


@callback_util.hold_bare_answers
async def settings_press_handler(event):
    """A press on the panel: saves the change, says so, redraws the panel."""
    if not await util.isAdmin(event):
        await event.answer(ADMINS_ONLY)
        return
    data = event.data.decode("utf-8", "replace")
    change = setting_change(data.removeprefix(SETTINGS_CALLBACK_PREFIX).split(":"))
    if change is None:
        await event.answer(f"{OUTDATED_BUTTON} Send /settings again.")
        return
    saved = _save_change(event.sender_id, change)
    await event.answer(change.summary if saved else SETTINGS_NOT_SAVED)
    prefs = shell_settings.SETTINGS.get(event.sender_id)
    try:
        await event.edit(
            _settings_text(prefs),
            parse_mode="md",
            link_preview=False,
            buttons=_settings_rows(prefs),
        )
    except errors.MessageNotModifiedError:
        pass


async def help_handler(event):
    if not await _is_admin_command(event):
        return
    await event.reply(
        HELP_TEXT.format(username=borg.me.username),
        parse_mode="md",
        link_preview=False,
    )


def _bot_command_pattern(command) -> str:
    """`/COMMAND`, also as `/COMMAND@thisbot`, with optional arguments."""
    mention = f"(?:@{re.escape(borg.me.username)})?" if borg.me.username else ""
    return rf"(?i)^/{command}{mention}(?:\s+(?P<args>.*))?\s*$"


def register_bot_handlers():
    borg.on(events.NewMessage(pattern=_bot_command_pattern("settings")))(
        settings_handler
    )
    borg.on(events.NewMessage(pattern=_bot_command_pattern("help")))(help_handler)
    borg.on(events.CallbackQuery(pattern=re.escape(SETTINGS_CALLBACK_PREFIX).encode()))(
        settings_press_handler
    )


if borg.me.bot:
    register_bot_handlers()
    borg.loop.create_task(bot_util.register_bot_commands(borg, BOT_COMMANDS))


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
