# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.

from uniborg.constants import (
    BOT_META_INFO_PREFIX,
    DEFAULT_FILE_LENGTH_THRESHOLD,
    DEFAULT_FILE_ONLY_LENGTH_THRESHOLD,
    CHAT_TITLE_MODEL,
    TWIN_FILE_MARKER,
)
from pynight.common_files import sanitize_filename
import json
from pydantic import BaseModel, Field
from brish import z, zp, zs, bsh, Brish

try:
    from brish import BrishWorkerDiedException
except ImportError:
    #: brish 0.3.5 (PyPI) has no such exception; an empty tuple catches nothing.
    BrishWorkerDiedException = ()
try:
    from brish import BrishCancelledException
except ImportError:
    #: brish before 0.4.1 takes no `cancelled=`, so nothing raises it.
    BrishCancelledException = ()
#: Whether the installed brish can stream a command's output (0.4.0 and later).
BRISH_POPEN = hasattr(Brish, "popen")
from pynight.common_icecream import ic
from collections.abc import Awaitable, Callable, Iterable
from IPython.terminal.embed import InteractiveShellEmbed, InteractiveShell
from IPython.terminal.ipapp import load_default_config
from aioify import aioify
import functools
from functools import partial
import inspect
import uuid
import asyncio
import subprocess
import traceback
import os
import pexpect
import re
import itertools
import shutil
import signal
import threading
from uniborg import util
from uniborg import guest_util
from uniborg import shell_stream
import telethon
from telethon import TelegramClient, events
import telethon.utils
from telethon.tl.functions.messages import GetPeerDialogsRequest
from telethon.tl.types import DocumentAttributeAudio
from telethon.errors.rpcerrorlist import PhotoExtInvalidError
from IPython import embed
import IPython
import sys
import pathlib
from pathlib import Path
import typing
from dataclasses import dataclass
from concurrent.futures import ThreadPoolExecutor
import io
from io import BytesIO
import tempfile
from enum import Enum


class SendFileMode(Enum):
    """Mode for file sending behavior."""

    ONLY = "only"  # Send as file instead of text (discreet_send behavior)
    ALSO = "also"  # Send as both text and file (edit_message behavior)
    ALSO_IF_LESS_THAN = "also_if_less_than"  # Send as both text and file only if text length is less than file_only_threshold
    NEVER = "never"  # Never send as file (text only)


try:
    import PIL
    import PIL.Image
    import PIL.ImageOps
except ImportError:
    PIL = None

import aiofiles
import aiofiles.os


##
def _resize_photo_if_needed(
    file,
    is_image,
    min_width=128,
    min_height=128,
    width=2560,
    height=2560,
    background=(255, 255, 255),
):
    #: [[zf:~\[site-packages\]/telethon/client/uploads.py::def _resize_photo_if_needed(]]
    #: forked from the original
    ##
    # print("_resize_photo_if_needed entered")

    # https://github.com/telegramdesktop/tdesktop/blob/12905f0dcb9d513378e7db11989455a1b764ef75/Telegram/SourceFiles/boxes/photo_crop_box.cpp#L254
    if (
        not is_image
        or PIL is None
        or (isinstance(file, io.IOBase) and not file.seekable())
    ):
        return file

    if isinstance(file, bytes):
        file = io.BytesIO(file)

    before = file.tell() if isinstance(file, io.IOBase) else None

    try:
        # Don't use a `with` block for `image`, or `file` would be closed.
        # See https://github.com/LonamiWebs/Telethon/issues/1121 for more.
        image = PIL.Image.open(file)
        try:
            kwargs = {"exif": image.info["exif"]}
        except KeyError:
            kwargs = {}

        too_small = image.width < min_width or image.height < min_height
        # print(f"_resize_photo_if_needed: too_small {too_small}")
        if (
            too_small
        ):  # the true issue is the aspect ratio, see https://github.com/LonamiWebs/Telethon/pull/1718
            image = PIL.ImageOps.pad(
                image,
                (max(image.width, min_width), max(image.height, min_height)),
                color="white",
            )
        else:
            if image.width <= width and image.height <= height:
                return file

            image.thumbnail((width, height), PIL.Image.LANCZOS)

        alpha_index = image.mode.find("A")
        if alpha_index == -1:
            # If the image mode doesn't have alpha
            # channel then don't bother masking it away.
            result = image
        else:
            # We could save the resized image with the original format, but
            # JPEG often compresses better -> smaller size -> faster upload
            # We need to mask away the alpha channel ([3]), since otherwise
            # IOError is raised when trying to save alpha channels in JPEG.
            result = PIL.Image.new("RGB", image.size, background)
            result.paste(image, mask=image.split()[alpha_index])

        buffer = io.BytesIO()
        result.save(buffer, "JPEG", **kwargs)
        buffer.name = "a.jpg"
        #: `.name` needs to be set for newer Telethon versions

        buffer.seek(0)
        return buffer

    except IOError:
        return file
    finally:
        if before is not None:
            file.seek(before, io.SEEK_SET)


telethon.client.uploads._resize_photo_if_needed = _resize_photo_if_needed
##
dl_base = os.getcwd() + "/dls/"
# pexpect_ai = aioify(obj=pexpect, name='pexpect_ai')
pexpect_ai = aioify(pexpect)
# os_aio = aioify(obj=os, name='os_aio')
os_aio = aioify(os)
# subprocess_aio = aioify(obj=subprocess, name='subprocess_aio')
subprocess_aio = aioify(subprocess)
borg: TelegramClient = None  # is set by init
##
admins = [
    # "Arstar",
    195391705,
]
admins_injected = os.environ.get("borg_admins", None)
if admins_injected:
    admins_injected = admins_injected.split(",")
    for admin in admins_injected:
        try:
            admin = int(admin)
        except:
            pass
        print(f"Admin added: {admin}", file=sys.stderr)
        admins.append(admin)

# Use chatids instead. Might need to prepend -100.
adminChats = [
    "1353500128",
    "1185370891",  # HEART
    "1600457131",  # This Anime Does not Exist
]
##
brish_count = int(os.environ.get("borg_brish_count", 16))
#: Workers of the plugins' own pool (`plugin_brish`).
plugin_brish_count = int(os.environ.get("borg_plugin_brish_count", 4))
executor = ThreadPoolExecutor(max_workers=(brish_count + plugin_brish_count + 16))


def force_async(f):
    @functools.wraps(f)
    def inner(*args, **kwargs):
        loop = asyncio.get_running_loop()
        return loop.run_in_executor(None, lambda: f(*args, **kwargs))

    return inner


# @force_async


async def za(template, *args, bsh=bsh, getframe=1, locals_=None, **kwargs):
    # @todo1 move this to brish itself
    loop = asyncio.get_running_loop()
    locals_ = locals_ or sys._getframe(getframe).f_locals

    def h_z():
        # can't get the previous frames in here, idk why
        cmd = bsh.zstring(template, locals_=locals_)
        return bsh.send_cmd(cmd, *args, **kwargs)

    future = loop.run_in_executor(None, h_z)

    return await future


def brish_server_cleanup(brish_server):
    if brish_server:
        brish_server.cleanup()


BRISH_BOOT_CMD = "export JBRISH=y ; unset FORCE_INTERACTIVE"

#: The shell's pool: `.a`, `.af` and the guest shell run here.
persistent_brish = None
#: The pool of every other plugin; see `plugin_brish`.
_plugin_brish = None
_plugin_brish_lock = threading.Lock()


def plugin_brish():
    """The Brish pool of plugins other than the shell, started on first use.

    With brish 0.4.0, a pool restarts after one of its workers dies (a
    stopped command that needed SIGKILL, or `exit N` in a non-fork command),
    and that restart waits for every command still running on it, an
    endless one included. With a pool of their own, a restart of the shell's
    pool never stalls the plugins, and theirs never stalls the shell. Brish
    0.4.1 replaces only the dead worker, so there the two pools only keep
    the shell's commands and the plugins' apart.
    """
    global _plugin_brish
    with _plugin_brish_lock:
        if _plugin_brish is None:
            _plugin_brish = Brish(
                boot_cmd=BRISH_BOOT_CMD, server_count=plugin_brish_count
            )
        return _plugin_brish


def init_brishes():
    """Starts a fresh shell pool and retires the old pools in the background.

    The old pools are captured now: `executor` is also the event loop's
    default executor, so a cleanup can wait behind running commands, and by
    then `persistent_brish` is the new pool. The plugin pool is not started
    again here; `plugin_brish` starts it at its next use.
    """
    print(f"Initializing {brish_count} brishes ...")
    global persistent_brish, _plugin_brish

    old_brish = persistent_brish
    persistent_brish = Brish(boot_cmd=BRISH_BOOT_CMD, server_count=brish_count)
    with _plugin_brish_lock:
        old_plugin_brish, _plugin_brish = _plugin_brish, None
    for old in (old_brish, old_plugin_brish):
        if old is not None:
            executor.submit(brish_server_cleanup, old)
    ##
    # global brishes
    # brishes = [Brish(boot_cmd=boot_cmd) for i in range(brish_count)] # range includes 0
    ##


init_brishes()


def restart_brishes():
    init_brishes()


def admin_cmd(pattern, outgoing="Ignored", additional_admins=[]):
    # return events.NewMessage(outgoing=True, pattern=re.compile(pattern))

    # chats doesn't work with this. (What if we prepend with -100?)
    # return events.NewMessage(chats=adminChats, from_users=admins, forwards=False, pattern=re.compile(pattern))

    # IDs should be an integer (not a string) or Telegram will assume they are phone numbers
    return events.NewMessage(
        from_users=([borg.me] + admins + additional_admins),
        forwards=False,
        pattern=re.compile(pattern),
        #: A bot's own guest answer comes back as an outgoing message from
        #: `borg.me`, with text the caller may have steered.
        func=_is_not_guest_answer,
    )


def _is_not_guest_answer(event) -> bool:
    return not guest_util.is_guest_answer(getattr(event, "message", None))


def _chat_may_grant_admin(chat, *, sender_id) -> bool:
    """Whether `chat` may vouch for its members through `adminChats` or its username.

    Groups and channels may. A private chat's "chat" is the other party: the
    sender itself in a bot's DM, where vouching changes nothing, but someone
    else in a userbot's DM or in a private guest chat, who must not vouch for
    the sender.
    """
    if isinstance(chat, telethon.tl.types.User):
        return chat.id == sender_id
    return True


def interact(local=None):
    if local is None:
        local = locals()
    import code

    code.interact(local=local)


def embed2(**kwargs):
    """Call this to embed IPython at the current point in your program.

    The first invocation of this will create an :class:`InteractiveShellEmbed`
    instance and then call it.  Consecutive calls just call the already
    created instance.

    If you don't want the kernel to initialize the namespace
    from the scope of the surrounding function,
    and/or you want to load full IPython configuration,
    you probably want `IPython.start_ipython()` instead.

    Here is a simple example::

        from IPython import embed
        a = 10
        b = 20
        embed(header='First time')
        c = 30
        d = 40
        embed()

    Full customization can be done by passing a :class:`Config` in as the
    config argument.
    """
    ix()  # MYCHANGE
    config = kwargs.get("config")
    header = kwargs.pop("header", "")
    compile_flags = kwargs.pop("compile_flags", None)
    if config is None:
        config = load_default_config()
        config.InteractiveShellEmbed = config.TerminalInteractiveShell
        kwargs["config"] = config
    using = kwargs.get("using", "asyncio")  # MYCHANGE
    if using:
        kwargs["config"].update(
            {
                "TerminalInteractiveShell": {
                    "loop_runner": using,
                    "colors": "NoColor",
                    "autoawait": using != "sync",
                }
            }
        )
    # save ps1/ps2 if defined
    ps1 = None
    ps2 = None
    try:
        ps1 = sys.ps1
        ps2 = sys.ps2
    except AttributeError:
        pass
    # save previous instance
    saved_shell_instance = InteractiveShell._instance
    if saved_shell_instance is not None:
        cls = type(saved_shell_instance)
        cls.clear_instance()
    frame = sys._getframe(1)
    shell = InteractiveShellEmbed.instance(
        _init_location_id="%s:%s" % (frame.f_code.co_filename, frame.f_lineno), **kwargs
    )
    shell(
        header=header,
        stack_depth=2,
        compile_flags=compile_flags,
        _call_location_id="%s:%s" % (frame.f_code.co_filename, frame.f_lineno),
    )
    InteractiveShellEmbed.clear_instance()
    # restore previous instance
    if saved_shell_instance is not None:
        cls = type(saved_shell_instance)
        cls.clear_instance()
        for subclass in cls._walk_mro():
            subclass._instance = saved_shell_instance
    if ps1 is not None:
        sys.ps1 = ps1
        sys.ps2 = ps2


ix_flag = False


def ix():
    global ix_flag
    if not ix_flag:
        import nest_asyncio

        nest_asyncio.apply()
        ix_flag = True


def embeda(locals_=None):
    # Doesn't work
    ix()
    if locals_ is None:
        previous_frame = sys._getframe(1)
        previous_frame_locals = previous_frame.f_locals
        locals_ = previous_frame_locals
        IPython.start_ipython(user_ns=locals_)


async def isAdmin(
    event, admins=admins, adminChats=adminChats, additional_admins=[], msg=None
):
    try:
        if additional_admins:
            admins = admins + additional_admins

        msg = msg or getattr(event, "message", None)
        if guest_util.is_guest_answer(msg):
            #: Our own guest answer, echoed back into a group: it is outgoing
            #: and sent by us, but its text may be the caller's doing.
            return False

        sender = getattr(msg, "sender", None) if msg else None
        sender = sender or getattr(event, "sender", None)
        sender_username = getattr(sender, "username", None)

        if guest_util.is_guest_message(msg) or guest_util.is_guest_event(event):
            #: A guest query's trigger: only its sender counts. A private
            #: trigger arrives with `out` set, and its chat is the other
            #: participant, so neither says anything about the caller.
            caller_id = (
                guest_util.caller_id_of(msg)
                if msg is not None
                else getattr(event, "sender_id", None)
            )
            return caller_id in admins or (
                sender_username is not None and sender_username in admins
            )

        sender_id = getattr(sender, "id", None) or getattr(event, "sender_id", None)
        sender_is_admin = (
            getattr(sender, "is_self", False)
            or sender_id in admins
            or sender_username in admins
        )
        res = sender_is_admin

        if msg:
            res = res or (getattr(msg, "out", False))

        if event:
            chat = None
            try:
                chat = await event.get_chat()
            except:
                pass

            if chat and _chat_may_grant_admin(chat, sender_id=sender_id):
                #: Doesnt work with private channels' links
                res = (
                    res
                    or (str(chat.id) in adminChats)
                    or (getattr(chat, "username", "NA") in admins)
                )

                # ix()
                # embed(using='asyncio')
                # embed2()

        return res

    except:
        borg._logger.warn(traceback.format_exc())
        return False


def is_admin_by_id(user_id: int, admins=admins, additional_admins=[]) -> bool:
    """Check if a user ID is in the admin list (non-async version)."""
    try:
        all_admins = admins + additional_admins
        return user_id in all_admins
    except:
        return False


async def is_read(borg, entity, message, is_out=None):
    """
    Returns True if the given message (or id) has been read
    if a id is given, is_out needs to be a bool
    """
    is_out = getattr(message, "out", is_out)
    if not isinstance(is_out, bool):
        raise ValueError("Message was id but is_out not provided or not a bool")
    message_id = getattr(message, "id", message)
    if not isinstance(message_id, int):
        raise ValueError("Failed to extract id from message")

    dialog = (await borg(GetPeerDialogsRequest([entity]))).dialogs[0]
    max_id = dialog.read_outbox_max_id if is_out else dialog.read_inbox_max_id
    return message_id <= max_id


async def run_and_get(
    event,
    to_await,
    cwd=None,
    *,
    delete_p=True,
    messages=None,
):
    """Downloads the media of a request into `cwd`, then awaits `to_await(cwd=, event=)`.

    By default the media come from `event.message`, the message it replies to,
    and their album siblings, fetched from the chat. With `messages`, exactly
    those are downloaded and nothing is fetched: a guest query's messages
    cannot be looked up in their chat. `event` may then be None.

    Downloads that `to_await` left unmodified are deleted afterwards when
    `delete_p`. Returns `cwd`.
    """
    if cwd is None:
        cwd = dl_base + str(uuid.uuid4()) + "/"
    Path(cwd).mkdir(parents=True, exist_ok=True)
    a = borg
    dled_files = []

    async def dl(z):
        if z is not None and getattr(z, "file", None) is not None:
            #: The name comes from the sender; keep only its last component.
            dled_file_name = Path(getattr(z.file, "name", "") or "").name
            dled_file_name = dled_file_name or f"some_file_{uuid.uuid4().hex}"
            dled_path = f"{cwd}{z.id}_{dled_file_name}"
            dled_path = await a.download_media(
                message=guest_util.download_target(z), file=dled_path
            )
            mdate = os.path.getmtime(dled_path)
            dled_files.append((dled_path, mdate, dled_file_name))

    if messages is not None:
        todl_map = {m.id: m for m in messages if m is not None}
    else:
        todl_map = await _messages_to_download(event)

    #: Iterate over the values of the dictionary to get the unique Message objects.
    todl_messages = list(todl_map.values())
    todl_messages.sort(key=lambda msg: msg.id)  #: sorts inplace
    for msg in todl_messages:
        await dl(msg)

    # ic(cwd, dled_files)

    await to_await(cwd=cwd, event=event)

    if delete_p:
        for dled_path, mdate, _ in dled_files:
            if os.path.exists(dled_path) and mdate == os.path.getmtime(dled_path):
                await remove_potential_file(dled_path, event)
    return cwd


async def _messages_to_download(event) -> dict:
    """`event.message`, its replied-to message, and their album siblings, by id."""
    a = borg
    #: Use a dictionary to store unique messages, with message.id as the key.
    todl_map = {event.message.id: event.message}
    inspection_list = [event.message]
    processed_group_ids = set()
    k = 30

    rep_id = event.message.reply_to_msg_id
    if rep_id:
        replied_message = await a.get_messages(event.chat, ids=rep_id)
        if replied_message:
            todl_map[replied_message.id] = replied_message
            inspection_list.append(replied_message)

    for message_to_inspect in inspection_list:
        if message_to_inspect and message_to_inspect.grouped_id:
            group_id = message_to_inspect.grouped_id
            if group_id in processed_group_ids:
                continue

            search_ids = range(message_to_inspect.id - k, message_to_inspect.id + k)
            messages_in_vicinity = await a.get_messages(
                event.chat, ids=list(search_ids)
            )

            for msg in messages_in_vicinity:
                if msg and msg.grouped_id == group_id:
                    #: Add message to the map; duplicates are automatically handled by the key.
                    todl_map[msg.id] = msg

            processed_group_ids.add(group_id)

    return todl_map


async def handle_exc(event, reply_exc=True):
    #: `reply_exc` should be False for bots facing random users (as opposed to admins).
    ##
    exc = "Julia encountered an exception. :(\n" + traceback.format_exc()
    await send_output(event, exc, shell=(reply_exc), retcode=1)


async def handle_exc_chat(chat, reply_exc=True):
    # @todo2 refactor send_output to work with just a chat, not an event
    exc = "Julia encountered an exception. :(\n" + traceback.format_exc()
    await borg.send_message(chat, exc)


def _sent_messages(result) -> list:
    """`borg.send_file` returns one message, or a list for an album."""
    if result is None:
        return []
    return list(result) if isinstance(result, list) else [result]


async def send_files(chat, files, **kwargs):
    """Sends `files` to `chat`, in one album per extension. Returns what was sent."""
    sent = []
    if isinstance(files, str) or not isinstance(files, Iterable):
        try:
            sent += _sent_messages(
                await borg.send_file(chat, files, allow_cache=False, **kwargs)
            )
        except:
            await handle_exc_chat(chat)
        return sent

    f2ext = lambda p: p.suffix
    files = [Path(f) for f in files]  # idempotent
    files = sorted(files, key=f2ext)
    for ext, fs in itertools.groupby(files, f2ext):  # groupby assumes sorted
        print(f"Sending files of '{ext}':")
        async with borg.action(chat, "document") as action:
            try:
                fs = list(fs)
                fs.sort()
                [print(f) for f in fs]
                print()
                # Use no-album workaround for GIFs or when album sending fails
                use_no_album = ext == ".gif"
                if not use_no_album:
                    try:
                        sent += _sent_messages(
                            await borg.send_file(chat, fs, allow_cache=False, **kwargs)
                        )
                    except PhotoExtInvalidError:
                        print(
                            f"Album sending failed, using no-album workaround. Files: {fs}"
                        )
                        use_no_album = True

                if use_no_album:
                    for f in fs:
                        sent += _sent_messages(
                            await borg.send_file(chat, f, allow_cache=False, **kwargs)
                        )
            except:
                await handle_exc_chat(chat)
    return sent


async def upload_output_files(chat, files, *, album_mode, reply_to=None, on_error):
    """Uploads the files a command left behind to `chat`. Returns the sent messages.

    With `album_mode`, and unless there is exactly one file, the files go out
    in one album per extension (`send_files`). Otherwise they go one at a time
    as replies to `reply_to`, and a name prefix picks how: `voicenote-`,
    `videonote-`, `fdoc-` (as a document) or `streaming-`. Directories are
    skipped. `on_error()` is awaited, inside the `except`, for each failed send.
    """
    files = list(files)
    if album_mode and len(files) != 1:
        files = [p.absolute() for p in files if not p.is_dir()]
        return await send_files(chat, files)

    sent = []
    files.sort()
    for p in files:
        if p.is_dir():  # and not any(s in p.name for s in ('.torrent', '.aria2')):
            continue
        file_add = p.absolute()
        base_name = str(await os_aio.path.basename(file_add))
        voice_note = base_name.startswith("voicenote-")
        video_note = base_name.startswith("videonote-")
        force_doc = base_name.startswith("fdoc-")
        supports_streaming = base_name.startswith("streaming-")
        async with borg.action(chat, "document") as action:
            try:
                sent += _sent_messages(
                    await borg.send_file(
                        chat,
                        file_add,
                        voice_note=voice_note,
                        video_note=video_note,
                        supports_streaming=supports_streaming,
                        force_document=force_doc,
                        reply_to=reply_to,
                        allow_cache=False,
                    )
                )
                #                            progress_callback=action.progress)
                # caption=base_name)
            except:
                await on_error()
    return sent


async def run_and_upload(event, to_await, quiet=True, reply_exc=True, album_mode=True):
    cwd = ""
    # util.interact(locals())
    try:
        chat = await event.get_chat()
        try:
            await borg.send_read_acknowledge(chat, event.message)
        except:
            pass
        trying_to_dl = await util.discreet_send(
            event, "Julia is processing your request ...", event.message, quiet
        )
        cwd = await run_and_get(event=event, to_await=to_await)
        await upload_output_files(
            chat,
            Path(cwd).glob("*"),
            album_mode=album_mode,
            reply_to=event.message,
            on_error=partial(handle_exc, event, reply_exc),
        )
    except asyncio.CancelledError:
        #: A cancel (the client disconnecting, say) is not the request's
        #: failure: reporting it would send a traceback through a closing
        #: client, and swallowing it would keep the canceller waiting.
        raise
    except:
        await handle_exc(event, reply_exc)
    finally:
        await remove_potential_file(cwd, event)


async def safe_run(event, cwd, command):
    # await event.reply('bash -c "' + command + '"' + '\n' + cwd)
    # await pexpect_ai.run(command, cwd=cwd)
    await subprocess_aio.run(command, cwd=cwd)


@dataclass
class CommandResult:
    """What a command printed (stdout and stderr together) and its exit code."""

    output: str
    retcode: int


async def simple_run_capture(
    *, cwd, command, shell=True, job=None
) -> typing.Optional[CommandResult]:
    """Runs `command` (through zsh when `shell`) in `cwd` and captures it.

    Its input is empty: the bot's own stdin is no one's to type into, and a
    command reading it would wait forever.

    With a `shell_stream.ShellJob` (which needs `shell`), the output goes into
    `job.output` while the command runs, and the job can stop it; see
    `_stream_zsh`. The result is then None when the job was stopped before
    the command started.
    """
    if job is not None:
        if not shell:
            raise ValueError("streaming a command needs shell=True")
        return await _stream_zsh(cwd=cwd, command=command, job=job)

    sp = await subprocess_aio.run(
        command,
        shell=shell,
        cwd=cwd,
        text=True,
        executable="zsh" if shell else None,
        stdin=subprocess.DEVNULL,
        stderr=subprocess.STDOUT,
        stdout=subprocess.PIPE,
    )
    return CommandResult(output=sp.stdout, retcode=sp.returncode)


#: Seconds between the steps that stop a streamed `.aa` command.
ZSH_KILL_GRACE = 2.0
#: The steps: an interrupt, as Ctrl-C would send, then termination, then a
#: kill that cannot be caught.
ZSH_KILL_SIGNALS = (signal.SIGINT, signal.SIGTERM, signal.SIGKILL)


def _signal_group(pgid: int, sig) -> bool:
    """Sends SIG to process group PGID; False when no process is left in it."""
    try:
        os.killpg(pgid, sig)
    except (ProcessLookupError, PermissionError):
        return False
    return True


class _ProcessGroupKiller:
    """The kill hook of a streamed `.aa` command.

    The command runs in a session, and so a process group, of its own; the
    whole group (background jobs included) gets each signal of
    `ZSH_KILL_SIGNALS`, `grace` seconds apart, until the group is empty.
    Callable from any thread; the steps run on `loop`.

    The group's id is its leader's process id. Once the group is empty, the
    system may give that id to a new process, and a later step would signal
    whatever group it leads: so the steps end when `command_ended` finds the
    group empty, not a grace later.
    """

    def __init__(self, pgid: int, *, loop, grace: float = ZSH_KILL_GRACE):
        self.pgid = pgid
        self.loop = loop
        self.grace = grace
        self._started = False
        #: The group is gone: no step may signal its id again.
        self._finished = False
        self._next_step = None

    def __call__(self) -> None:
        try:
            self.loop.call_soon_threadsafe(self._start)
        except RuntimeError:
            #: The loop is closed; the producer's own cleanup has run.
            pass

    def command_ended(self) -> None:
        """On the loop, once the command's own process has been reaped.

        Ends the steps if nothing is left in the group. A background job left
        there keeps the id reserved, and gets the later steps.
        """
        if _signal_group(self.pgid, 0):
            return
        self._finished = True
        if self._next_step is not None:
            self._next_step.cancel()
            self._next_step = None

    def _start(self) -> None:
        if not (self._started or self._finished):
            self._started = True
            self._step(0)

    def _step(self, index: int) -> None:
        self._next_step = None
        if self._finished:
            return
        if not _signal_group(self.pgid, ZSH_KILL_SIGNALS[index]):
            self._finished = True
        elif index + 1 < len(ZSH_KILL_SIGNALS):
            self._next_step = self.loop.call_later(self.grace, self._step, index + 1)


async def _stream_zsh(*, cwd, command, job) -> typing.Optional[CommandResult]:
    """Runs `command` with `zsh -c`, writing its output into `job` as it comes.

    The argv is the one `subprocess.run(shell=True, executable="zsh")` uses.
    The output is read on the event loop, so a running `.aa` holds no
    executor thread: it still works when every thread is busy. The command
    gets a session of its own, so a stop reaches its whole process group,
    background jobs included (`_ProcessGroupKiller`). A cancelled await
    kills the group at once and re-raises.

    The result's output is `job.output.final_text(render=False)`: UTF-8 with
    `\\r\\n` and `\\r` turned into `\\n`, as `text=True` gives for valid output;
    invalid bytes become `\\xNN` escapes instead of a UnicodeDecodeError.
    """
    if not job.try_start():
        return None
    job.output.decoding = shell_stream.Decoding(translate_newlines=True)
    proc = await asyncio.create_subprocess_exec(
        "zsh",
        "-c",
        command,
        cwd=cwd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    killer = _ProcessGroupKiller(proc.pid, loop=asyncio.get_running_loop())
    job.attach(killer)
    try:
        while chunk := await proc.stdout.read(65536):
            job.output.write(chunk)
        retcode = await proc.wait()
    except asyncio.CancelledError:
        job.cancel(reason=shell_stream.StopReason.SHUTDOWN)
        _signal_group(proc.pid, signal.SIGKILL)
        raise
    finally:
        job.detach()
        killer.command_ended()
    return CommandResult(output=job.output.final_text(render=False), retcode=retcode)


async def simple_run(event, cwd, command, shell=True):
    result = await simple_run_capture(cwd=cwd, command=command, shell=shell)
    await send_output(event, result.output, retcode=result.retcode, shell=shell)


def shell_output_text(output: str, *, retcode) -> str:
    """What `.a` shows for OUTPUT: trimmed, or "The process exited N." if empty."""
    output = output.strip()
    return f"The process exited {retcode}." if output == "" else output


async def send_output(event, output: str, retcode=-1, shell=True):
    output = shell_output_text(output, retcode=retcode)
    if not shell:
        print(output)
        if retcode != 0:
            output = "Something went wrong. Try again tomorrow. If the issue persists, file an issue on https://github.com/NightMachinary/betterborg and include the input that caused the bug."
        else:
            output = ""
    await discreet_send(event, output, event.message)


async def remove_potential_file(file, event=None):
    try:
        if os.path.exists(file):
            if os.path.isfile(file):
                os.remove(file)
            else:
                shutil.rmtree(file)
    except:
        if event is not None:
            await event.reply(
                "Julia encountered an exception. :(\n" + traceback.format_exc()
            )


def _check_split_candidate(text: str, i: int) -> tuple[bool, int]:
    """Check if position i is a valid split point and return (is_valid, split_position).

    Returns:
        (True, position) if valid split point found
        (False, 0) if not a valid split point
    """
    if i >= len(text):
        return False, 0

    # Line breaks (highest priority)
    if text[i] == "\n":
        return True, i + 1

    # Sentence boundaries
    if text[i] in ".!?" and i + 1 < len(text) and text[i + 1] == " ":
        return True, i + 1

    # Other punctuation
    if text[i] in ",;:" and i + 1 < len(text) and text[i + 1] == " ":
        return True, i + 1

    # Word boundaries (spaces) - lowest priority
    if text[i] in " \t":
        # Skip consecutive spaces and return position after the last space
        j = i
        while j + 1 < len(text) and text[j + 1] in " \t":
            j += 1
        return True, j + 1

    return False, 0


#: How far back from a chunk's limit the splitter looks for a good place to
#: break (a newline, a sentence's end, a space). With the forward search that
#: `edit_message` uses, a text longer than `max_len` less this can become two
#: messages although it would fit in one; a shorter one never does.
SPLIT_SEARCH_CHARS = 600


def _find_best_split_point(
    text: str,
    start_pos: int,
    max_length: int,
    *,
    search_direction: int = -1,
    buffer_size=SPLIT_SEARCH_CHARS,
) -> int:
    """Find the best position to split text, prioritizing word boundaries and markdown preservation.

    Args:
        text: The text to split
        start_pos: Starting position in the text
        max_length: Maximum length of the chunk
        search_direction: -1 for backward search (better quality splits),
                         0 for forward search (streaming-consistent)
    """
    end_pos = start_pos + max_length
    end_pos = min(end_pos, len(text))
    text_len = len(text) - start_pos

    # Determine search range based on direction
    if search_direction == 0:
        if text_len + buffer_size <= max_length:
            return end_pos

        # Forward search for streaming consistency
        min_pos = max(start_pos, end_pos - buffer_size)
        search_range = range(min_pos, end_pos)
    else:
        # Backward search for better quality splits
        if text_len <= max_length:
            return end_pos

        search_start = end_pos - 1
        search_limit = max(start_pos, search_start - buffer_size)
        search_range = range(search_start, search_limit - 1, -1)

    # Define split strategies in priority order
    def try_strategies(search_range):
        # Strategy 1: Look for newlines first (highest priority)
        for i in search_range:
            if text[i] == "\n":
                split_pos = i + 1
                return split_pos

        # Strategy 2: Look for sentence boundaries

        #: Early return. We might find a newline if the text grows later.
        if search_direction == 0 and text_len + int(buffer_size * 0.3) <= max_length:
            return end_pos

        for i in search_range:
            if text[i] in ".!?" and i + 1 < len(text) and text[i + 1] == " ":
                split_pos = i + 1
                return split_pos

        # Strategy 3: Look for other punctuation
        for i in search_range:
            if text[i] in ",;:" and i + 1 < len(text) and text[i + 1] == " ":
                split_pos = i + 1
                return split_pos

        #: Early return. We might fit a better strategy if the text grows later.
        if search_direction == 0 and text_len + int(buffer_size * 0.1) <= max_length:
            return end_pos

        # Strategy 4: Look for word boundaries (spaces) - lowest priority
        for i in search_range:
            if text[i] in " \t":
                # Skip consecutive spaces and return position after the last space
                j = i
                while j + 1 < len(text) and text[j + 1] in " \t":
                    j += 1
                split_pos = j + 1
                return split_pos

        return None

    # Try to find a split point using the strategies
    result = try_strategies(search_range)
    if result is not None:
        return result

    # Last resort: use the max position
    return end_pos


def _split_message_smart(
    message: str,
    *,
    max_chunk_size: int = 4000,
    search_direction: int = -1,
) -> list[str]:
    """Split message into chunks with smart boundary detection.

    Args:
        message: The text to split
        max_chunk_size: Maximum size of each chunk
        search_direction: -1 for backward search (better quality), 0 for forward (streaming-consistent)
    """
    if not message:
        return []

    chunks = []
    pos = 0

    while pos < len(message):
        # Find the best split point
        split_pos = _find_best_split_point(
            message, pos, max_chunk_size, search_direction=search_direction
        )

        # Ensure we make progress
        if split_pos <= pos:
            split_pos = min(pos + max_chunk_size, len(message))

        # Extract the chunk
        chunk = message[pos:split_pos].rstrip()
        if chunk:
            chunks.append(chunk)

        # Skip any whitespace at the split position for the next chunk
        while split_pos < len(message) and message[split_pos] in " \t":
            split_pos += 1

        pos = split_pos

    return chunks


async def discreet_send(
    event,
    message,
    reply_to=None,
    quiet=False,
    link_preview=False,
    parse_mode=None,
    *,
    send_file_mode=SendFileMode.ONLY,
    file_length_threshold=DEFAULT_FILE_LENGTH_THRESHOLD,
    file_only_threshold=DEFAULT_FILE_ONLY_LENGTH_THRESHOLD,
    file_name_mode="random",
    title_model: str | None = None,
    api_keys: dict | None = None,
    api_user_id: int | None = None,
):
    """
    Send a message, splitting it into chunks if needed or sending as file.

    Args:
        send_file_mode: SendFileMode.ONLY to send as file instead of text,
                       SendFileMode.ALSO to send as both text and file,
                       SendFileMode.ALSO_IF_LESS_THAN to send as both text and file only if length < file_only_threshold,
                       SendFileMode.NEVER to always send as text (never as file).
        file_length_threshold: If int, send as file when length >= threshold.
                              If bool-like, always/never send as file.
        file_only_threshold: For ALSO_IF_LESS_THAN mode, threshold below which to send both text and file.
        file_name_mode: File naming mode - "random", "timestamp", or "llm".
        title_model (str | None): Optional override of the model used to
            generate a smart filename when file_name_mode == "llm".
        api_keys (dict | None): Optional mapping of service name (e.g., "gemini") to
            API key value. If provided, avoids sender_id-based key lookup.
    """
    message = message.strip()
    if quiet or len(message) == 0:
        return reply_to

    # Use shared helper to determine text/file decisions
    decision = _should_send_as_file(
        message, file_length_threshold, send_file_mode, file_only_threshold
    )

    if decision.send_file:
        chat = await event.get_chat()
        async with borg.action(chat, "document") as action:
            file_data = await _generate_file_data(
                message,
                parse_mode,
                file_name_mode,
                title_model=title_model,
                api_keys=api_keys,
                api_user_id=api_user_id,
                message_obj=getattr(event, "message", None),
            )

            # Send file using existing function
            file_message = await send_text_as_file(
                text=message,
                suffix=file_data.suffix,
                chat=chat,
                caption=file_data.caption,
                reply_to=reply_to,
                filename=file_data.filename,
            )

        # If we should not send text, return the file message
        if not decision.send_text:
            return file_message
        # Otherwise, continue to send text message as well

    # Use smart splitting for shorter messages
    if not decision.send_text:
        return reply_to

    chunks = _split_message_smart(message)
    last_msg = reply_to

    for i, chunk in enumerate(chunks):
        last_msg = await event.respond(
            chunk,
            link_preview=link_preview,
            reply_to=(reply_to if i == 0 else last_msg),
            parse_mode=parse_mode,
        )

    return last_msg


def discreet_sends_file(text: str) -> bool:
    """Whether `discreet_send`, with its default thresholds, sends TEXT as a file."""
    return _should_send_as_file(
        text.strip(),
        DEFAULT_FILE_LENGTH_THRESHOLD,
        SendFileMode.ONLY,
        DEFAULT_FILE_ONLY_LENGTH_THRESHOLD,
    ).send_file


def _utf16_units(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def split_paragraphs(text: str, *, max_units: int = 4000) -> list[str]:
    """Packs whole paragraphs (blank-line separated) into chunks of `max_units`.

    Units are UTF-16 code units of the source text, an upper bound on what
    Telegram counts after parsing Markdown. A paragraph too long on its own is
    split by `_split_message_smart`.
    """
    chunks = []
    current = ""
    for paragraph in re.split(r"\n\s*\n", text.strip()):
        paragraph = paragraph.strip("\n")
        if not paragraph:
            continue
        candidate = f"{current}\n\n{paragraph}" if current else paragraph
        if _utf16_units(candidate) <= max_units:
            current = candidate
            continue
        if current:
            chunks.append(current)
        if _utf16_units(paragraph) <= max_units:
            current = paragraph
        else:
            #: Code points bound UTF-16 units from below, so halve for safety.
            *parts, current = _split_message_smart(
                paragraph, max_chunk_size=max_units // 2
            )
            chunks.extend(parts)
    if current:
        chunks.append(current)
    return chunks


async def reply_in_chunks(
    event,
    text: str,
    *,
    prefix: str = "",
    parse_mode=None,
    link_preview=False,
    max_units: int = 4000,
) -> list:
    """Replies with `text` as a chain of messages that each fit Telegram's limit.

    The first message replies to `event`, each later one to the one before.
    Every message starts with `prefix`, so a marker such as
    `BOT_META_INFO_PREFIX` holds for all of them. Returns the sent messages.
    """
    sent = []
    target = event
    for chunk in split_paragraphs(text, max_units=max_units - _utf16_units(prefix)):
        target = await target.reply(
            f"{prefix}{chunk}", parse_mode=parse_mode, link_preview=link_preview
        )
        sent.append(target)
    return sent


@dataclass(frozen=True)
class SentChunk:
    """The chunk one message of an edit chain shows, as we last sent it."""

    text: str
    parse_mode: typing.Any = None


@dataclass
class EditChainState:
    """Stores the state of an edit chain including children and last computed text."""

    children: list = None
    last_text: str = ""
    #: Message id -> the `SentChunk` that message shows, for the head and each child.
    #: Telethon's `Message.edit` returns a new object and leaves `.text` stale,
    #: so only this record can tell whether an edit would change anything.
    sent: dict = None
    #: The message that stands in for the original head once the chain was
    #: sent anew (see `send_new_on_head_failure`); None means the original.
    head: typing.Any = None

    def __post_init__(self):
        if self.children is None:
            self.children = []
        if self.sent is None:
            self.sent = {}

    def head_for(self, message_obj):
        """The message currently heading the chain that `message_obj` started."""
        return message_obj if self.head is None else self.head

    def forget_sent_except(self, messages):
        """Drop the records of messages that are no longer in the chain."""
        keep = {message.id for message in messages}
        self.sent = {
            msg_id: chunk for msg_id, chunk in self.sent.items() if msg_id in keep
        }


@dataclass
class FileGeneration:
    """Encapsulates all data needed for intelligent file generation."""

    filename: str
    caption: str
    extension: str

    @property
    def suffix(self) -> str:
        """Get the file extension for use with send_text_as_file."""
        return self.extension


@dataclass
class SendDecision:
    """Decision for whether to send text and/or file."""

    send_text: bool
    send_file: bool


# Helper functions for DRY improvements
def _should_send_as_file(
    text: str,
    file_length_threshold,
    send_file_mode: SendFileMode,
    file_only_threshold: int = DEFAULT_FILE_ONLY_LENGTH_THRESHOLD,
) -> SendDecision:
    """Return whether to send text and/or file based on mode and thresholds.

    - NEVER: send_text=True, send_file=False
    - ONLY: send_file if threshold says so; otherwise send_text. Never both.
    - ALSO: always send_text; send_file if threshold says so.
    - ALSO_IF_LESS_THAN: always send_file; send_text only if len(text) < file_only_threshold.
    - Empty/whitespace text: send_text=False, send_file=False
    """
    if isinstance(file_length_threshold, int) and isinstance(file_only_threshold, int):
        #: If at file_only_threshold, we are going to send only a file, then at that same length, we should always send a file.
        file_length_threshold = min(file_length_threshold, file_only_threshold)

    if not text or not text.strip():
        return SendDecision(send_text=False, send_file=False)

    def file_condition() -> bool:
        if isinstance(file_length_threshold, int):
            return len(text) >= file_length_threshold
        return bool(file_length_threshold)

    if send_file_mode == SendFileMode.NEVER:
        return SendDecision(send_text=True, send_file=False)

    if send_file_mode == SendFileMode.ONLY:
        should_file = file_condition()
        if should_file:
            return SendDecision(send_text=False, send_file=True)
        else:
            return SendDecision(send_text=True, send_file=False)

    if send_file_mode == SendFileMode.ALSO:
        return SendDecision(send_text=True, send_file=file_condition())

    if send_file_mode == SendFileMode.ALSO_IF_LESS_THAN:
        send_file = file_condition()
        if send_file:
            send_text = len(text) < file_only_threshold
        else:
            send_text = True

        # ic(send_file, send_text, len(text), file_length_threshold, file_only_threshold)
        return SendDecision(send_text=send_text, send_file=send_file)

    # Default fallback: behave like NEVER
    return SendDecision(send_text=True, send_file=False)


async def _safe_delete_message(message):
    """Safely delete a message, ignoring any errors."""
    try:
        await message.delete()
    except Exception:
        pass  # Ignore if deletion fails


def _chain_message_shows(edit_state, message, chunk: SentChunk) -> bool:
    """Whether `message` already shows `chunk`, going by what we last sent it.

    A message we have never edited (the placeholder, on the first call) has no
    record, and its own `.text` is still accurate, so it is compared instead.
    """
    shown = edit_state.sent.get(message.id)
    if shown is None:
        return message.text == chunk.text
    return shown == chunk


async def _edit_chain_message(edit_state, message, *, chunk, link_preview=None):
    """Edit `message` to show `chunk` unless it already does, and record it.

    `link_preview=None` leaves Telethon's default (the message's current
    preview state). Errors other than "not modified" propagate unrecorded.
    """
    if not _chain_message_shows(edit_state, message, chunk):
        edit_kwargs = {"parse_mode": chunk.parse_mode}
        if link_preview is not None:
            edit_kwargs["link_preview"] = link_preview
        try:
            await message.edit(chunk.text, **edit_kwargs)
        except telethon.errors.rpcerrorlist.MessageNotModifiedError:
            #: Telegram says it already shows this, e.g. markdown that renders
            #: the same, or a placeholder whose stale `.text` we compared.
            pass
    edit_state.sent[message.id] = chunk


async def _collapse_chain(edit_state, *, chain_key, head, placeholder):
    """Delete the children and leave only `placeholder` on the head, best-effort."""
    for child in edit_state.children:
        await _safe_delete_message(child)
    edit_state.children = []
    edit_state.last_text = ""
    edit_state.forget_sent_except([head])
    try:
        await _edit_chain_message(
            edit_state, head, chunk=SentChunk(placeholder, parse_mode="md")
        )
    except Exception:
        pass
    EDIT_CHAINS[chain_key] = edit_state


async def _reply_chain(parent, chunks, *, edit_state, parse_mode):
    """Send each chunk as a reply to the previous message, stopping at the first failure.

    Returns the messages sent, and records each in `edit_state.sent`.
    """
    sent_messages = []
    for chunk in chunks:
        try:
            parent = await parent.reply(chunk, parse_mode=parse_mode)
        except Exception:
            break
        edit_state.sent[parent.id] = SentChunk(chunk, parse_mode=parse_mode)
        sent_messages.append(parent)
    return sent_messages


#: Head-edit failures after which the head cannot show the text soon, or ever.
#: Telethon sleeps through a FloodWait of up to `flood_sleep_threshold` (60 s by
#: default) and raises only for longer ones. Sending is limited separately from
#: editing, so a new message usually still goes through.
HEAD_LOST_ERRORS = (
    telethon.errors.rpcerrorlist.FloodWaitError,
    telethon.errors.rpcerrorlist.MessageIdInvalidError,
    telethon.errors.rpcerrorlist.MessageAuthorRequiredError,
    telethon.errors.rpcerrorlist.MessageEditTimeExpiredError,
)


async def _resend_chain(
    edit_state,
    *,
    chunks,
    stale_head,
    reply_to,
    parse_mode,
    link_preview,
    delete_stale_head=True,
) -> bool:
    """Send `chunks` as a new chain, then delete the stale one, best-effort.

    The first chunk replies to `reply_to`, or else to whatever the stale head
    replied to (a reply to the stale head itself would point at a deleted
    message). `delete_stale_head=False` keeps the stale head and deletes only
    the children. On success, `edit_state` describes the new chain. Returns
    False, leaving everything as it was, when not even the first chunk can be
    sent.
    """
    anchor = reply_to if reply_to is not None else stale_head.reply_to_msg_id
    try:
        new_head = await stale_head.respond(
            chunks[0],
            reply_to=anchor,
            parse_mode=parse_mode,
            link_preview=link_preview,
        )
    except Exception as e:
        print(f"Error sending a replacement for message {stale_head.id}: {e}")
        return False

    stale_messages = [*edit_state.children]
    if delete_stale_head:
        stale_messages.append(stale_head)
    edit_state.head = new_head
    edit_state.sent = {new_head.id: SentChunk(chunks[0], parse_mode=parse_mode)}
    edit_state.children = await _reply_chain(
        new_head, chunks[1:], edit_state=edit_state, parse_mode=parse_mode
    )
    for message in stale_messages:
        await _safe_delete_message(message)
    return True


def _log_file_sending_error(context_name):
    """Log file sending error in a standardized way."""
    print(f"File sending failed in {context_name}:", file=sys.stderr)
    traceback.print_exc()


# Dictionary to track message chains for the edit_message function
# Key: `_edit_chain_key` of the original message, Value: EditChainState object
EDIT_CHAINS = {}


def _edit_chain_key(message_obj):
    """The `EDIT_CHAINS` key of the chain that `message_obj` started.

    A message id alone is ambiguous: every supergroup and channel numbers its
    own messages, so a bare id could pick up, and edit, another chat's chain.
    """
    return (getattr(message_obj, "chat_id", None), message_obj.id)


def forget_edit_chain(message_obj) -> None:
    """Drops what `edit_message` recorded of the chain `message_obj` started.

    For a message that `edit_message` will not edit again, such as a preview
    whose final was shown by other means, so the record does not outlive it.
    """
    EDIT_CHAINS.pop(_edit_chain_key(message_obj), None)


async def edit_message(
    message_obj,
    new_text,
    link_preview=False,
    parse_mode=None,
    max_len=4096,
    append_p=False,
    *,
    reply_to=None,
    send_file_mode=SendFileMode.NEVER,
    file_length_threshold=None,
    file_only_threshold=DEFAULT_FILE_ONLY_LENGTH_THRESHOLD,
    file_name_mode="random",
    title_model: str | None = None,
    api_keys: dict | None = None,
    title_generator: "TitleGenerator | None" = None,
    send_new_on_head_failure: bool = False,
    raise_on_head_failure: bool = False,
    twin_file_marker: str = TWIN_FILE_MARKER,
):
    """
    Intelligently edits a message chain to reflect new text content,
    avoiding redundant API calls.

    - Skips the edit of any message that already shows its chunk, going by the
      chunk and parse mode this function last sent it (`EditChainState.sent`),
      since Telethon never updates a message's `.text` in place. This assumes
      edit_message is the only writer of the chain: after a direct `.edit()` of
      one of its messages, re-sending the text edit_message last sent there is
      skipped.
    - Edits existing messages in the chain to match the new text.
    - Creates new messages if the new text is longer than the old chain.
    - Deletes surplus messages if the new text is shorter.
    - Tracks the relationship between the original message and its children.

    Args:
        message_obj: The original Telethon Message object to be edited.
        new_text (str): The new, potentially long, text content.
        link_preview (bool): Whether to enable link previews.
        parse_mode (str): The markdown parse mode.
        max_len (int): The maximum length of a single message.
        append_p (bool): If True, append new_text to existing content separated by BOT_META_INFO_LINE.
                        If False, replace existing content with new_text (default behavior).
        reply_to: Optional message to reply to when sending a file, or a new
            chain under `send_new_on_head_failure`.
        send_file_mode: SendFileMode.ALSO to also send as file in addition to text,
                       SendFileMode.ONLY to skip text editing and only send as file,
                       SendFileMode.ALSO_IF_LESS_THAN to send as both text and file only if length < file_only_threshold,
                       SendFileMode.NEVER to never send a file (text only).
        file_length_threshold: If int, send as file when length >= threshold.
                              If bool-like, always/never send as file.
        file_only_threshold: For ALSO_IF_LESS_THAN mode, threshold below which to send both text and file.
        file_name_mode (str): File naming mode - "random", "timestamp", or "llm".
        title_model (str | None): Optional override of the model used to
            generate a smart filename when file_name_mode == "llm". Defaults to
            constants.CHAT_TITLE_MODEL when not provided.
        api_keys (dict | None): Optional mapping of service name (e.g., "gemini") to
            API key value. If provided, avoids sender_id-based key lookup.
        title_generator (TitleGenerator | None): Writes the "llm" title and
            summary in place of `title_model` and `api_keys` (see
            `title_util.file_title_generator`).
        send_new_on_head_failure (bool): What to do when editing the head fails
            with one of `HEAD_LOST_ERRORS` (a FloodWait too long for Telethon to
            sleep through, or a head that is gone or no longer editable).
            If False (the default, meant for partial streaming edits), print the
            error and leave the chain as it was.
            If True (meant for final delivery, so the answer is not lost), send
            the text as a new chain: the first chunk replies to `reply_to` (else
            to what the head replied to) and each further chunk to the previous
            one; then delete the stale chain, best-effort (except a head that
            is not ours). The new chain is recorded as `message_obj`'s, so later
            calls with the same `message_obj` (say an `append_p` notice) edit
            the new chain and append to its text; the stale head is never
            touched again. A child edit that fails with one of these errors is
            covered too, since the head edit is skipped when the head already
            shows its chunk: that child and the ones after it are replaced by
            new replies to the last child kept.
            Other errors abort as with False.
        raise_on_head_failure (bool): If True, a failed head edit that
            `send_new_on_head_failure` does not send anew is raised instead of
            printed, so a caller can see that the message does not show the
            text (say, to back off from a deleted message). The chain is left
            as it was, as without it. If False (the default), this function
            prints the error and returns normally.
        twin_file_marker (str): Caption prefix for the file sent together with
            the text (`constants.TWIN_FILE_MARKER`), so history can skip that
            twin. It is added only when every chunk of the text was delivered:
            otherwise the file may be the only full copy. Pass "" to never mark.
            A file sent instead of the text is never marked.
    """
    global EDIT_CHAINS
    message_id = message_obj.id
    chain_key = _edit_chain_key(message_obj)

    new_text = new_text.strip()

    # Get or create the edit state for this message ID
    edit_state = EDIT_CHAINS.get(chain_key, EditChainState())
    head = edit_state.head_for(message_obj)

    # Handle append_p mode: append new_text to existing content
    if append_p:
        from uniborg.constants import BOT_META_INFO_LINE

        # Use the stored last_text instead of reconstructing from messages
        existing_text = edit_state.last_text
        existing_text = existing_text.strip() if existing_text else ""

        # Only append if there's existing text and new text
        if existing_text and new_text:
            new_text = f"{existing_text}\n\n{BOT_META_INFO_LINE}\n{new_text}"
        elif existing_text:
            # If no new text but existing text exists, keep existing
            new_text = existing_text
        # else: if no existing text, just use new_text as-is

    # Determine text/file decisions using shared helper
    decision = _should_send_as_file(
        new_text, file_length_threshold, send_file_mode, file_only_threshold
    )
    only_send_file = decision.send_file and not decision.send_text
    #: Whether the chain ends up showing every chunk of `new_text`.
    text_delivered = False

    # If we should skip text editing, clean up message chain and send file
    if only_send_file:
        await _collapse_chain(
            edit_state,
            chain_key=chain_key,
            head=head,
            placeholder="__[sent as file]__",
        )

        # Send the file
        try:
            await send_as_file_with_filename(
                text=new_text,
                parse_mode=parse_mode,
                file_name_mode=file_name_mode,
                message_obj=message_obj,
                reply_to=reply_to,
                title_model=title_model,
                api_keys=api_keys,
                title_generator=title_generator,
            )
        except Exception:
            _log_file_sending_error("edit_message (ONLY mode)")
        return

    try:
        # Chunk the new text with forward search for streaming consistency
        chunks = (
            _split_message_smart(
                new_text,
                max_chunk_size=max_len,
                search_direction=0,
            )
            if new_text
            else []
        )

        existing_children = edit_state.children
        new_children = []

        # Case 1: The new text is empty, delete the entire chain.
        if not chunks:
            await _collapse_chain(
                edit_state,
                chain_key=chain_key,
                head=head,
                placeholder="__[empty]__",
            )
            return

        # Edit the primary message (the one the user replied to)
        try:
            await _edit_chain_message(
                edit_state,
                head,
                chunk=SentChunk(chunks[0], parse_mode=parse_mode),
                link_preview=link_preview,
            )
        except Exception as e:
            send_new = send_new_on_head_failure and isinstance(e, HEAD_LOST_ERRORS)
            if raise_on_head_failure and not send_new:
                raise
            print(f"Error editing original message {message_id}: {e}")
            if send_new:
                if await _resend_chain(
                    edit_state,
                    chunks=chunks,
                    stale_head=head,
                    reply_to=reply_to,
                    parse_mode=parse_mode,
                    link_preview=link_preview,
                    #: Someone else's message: not ours to delete, even where
                    #: admin rights would let us.
                    delete_stale_head=not isinstance(
                        e, telethon.errors.rpcerrorlist.MessageAuthorRequiredError
                    ),
                ):
                    edit_state.last_text = new_text
                    EDIT_CHAINS[chain_key] = edit_state
                    text_delivered = len(edit_state.children) == len(chunks) - 1
            return  # If the head of the chain fails, abort

        # Now, handle the children (the rest of the chunks)
        chain_intact = True
        #: zip stops at the shorter list: the children that get a chunk.
        for child_to_edit, chunk in zip(existing_children, chunks[1:]):
            try:
                await _edit_chain_message(
                    edit_state,
                    child_to_edit,
                    chunk=SentChunk(chunk, parse_mode=parse_mode),
                    link_preview=link_preview,
                )
            except Exception as e:
                if send_new_on_head_failure and isinstance(e, HEAD_LOST_ERRORS):
                    #: The head of a long answer usually already shows its
                    #: chunk, so a final edit's FloodWait lands here instead.
                    #: This child and the rest are replaced below.
                    print(
                        f"Error editing message {child_to_edit.id} of chain {message_id}: {e}"
                    )
                else:
                    # If editing a child fails, stop processing the chain to avoid errors.
                    chain_intact = False
                break
            new_children.append(child_to_edit)

        if chain_intact:
            kept_count = len(new_children)
            # --- Send the chunks that no kept child shows ---
            new_children += await _reply_chain(
                new_children[-1] if new_children else head,
                chunks[1 + kept_count :],
                edit_state=edit_state,
                parse_mode=parse_mode,
            )

            # --- Delete the children not kept: surplus, or replaced above ---
            for child_to_delete in existing_children[kept_count:]:
                await _safe_delete_message(child_to_delete)
            text_delivered = len(new_children) == len(chunks) - 1

        # Update the global state with the new chain configuration
        edit_state.children = new_children
        edit_state.forget_sent_except([head, *new_children])
        edit_state.last_text = (
            new_text  # Store the last text for future append operations
        )

        if new_children or new_text:
            EDIT_CHAINS[chain_key] = edit_state
        else:
            EDIT_CHAINS.pop(chain_key, None)

    finally:
        # Send file after message editing (success or failure)
        if decision.send_file and decision.send_text:
            try:
                await send_as_file_with_filename(
                    text=new_text,
                    parse_mode=parse_mode,
                    file_name_mode=file_name_mode,
                    message_obj=message_obj,
                    reply_to=reply_to,
                    title_model=title_model,
                    api_keys=api_keys,
                    title_generator=title_generator,
                    caption_prefix=twin_file_marker if text_delivered else "",
                )
            except Exception:
                _log_file_sending_error("edit_message")


def postproccesor_json(file_path):
    (z("cat {file_path}").out)

    return z("cat {file_path} | command jq . | sponge {file_path}").assert_zero


async def send_text_as_file(
    text: str, *, suffix: str = ".txt", chat, postproccesors=[], filename=None, **kwargs
):
    f = tempfile.NamedTemporaryFile(suffix=suffix, delete=False)
    try:
        f_path = f.name
        # ic(f_path)

        f.write(text.encode())
        f.close()

        for postproccesor in postproccesors:
            postproccesor(f_path)

        # Handle filename attribute internally
        if filename:
            if "attributes" not in kwargs:
                kwargs["attributes"] = []
            elif kwargs["attributes"] is None:
                kwargs["attributes"] = []
            else:
                # Make a copy to avoid modifying the original list
                kwargs["attributes"] = list(kwargs["attributes"])

            kwargs["attributes"].append(
                telethon.tl.types.DocumentAttributeFilename(filename)
            )

        async with borg.action(chat, "document") as action:
            last_msg = await borg.send_file(
                chat,
                f_path,
                allow_cache=False,
                **kwargs,
            )

        return last_msg
    finally:
        await remove_potential_file(f)


def _get_title_model_and_service(title_model: str | None) -> tuple[str, str]:
    """Return (model_in_use, service_needed) for LLM title generation.

    - Uses provided title_model if set, otherwise falls back to CHAT_TITLE_MODEL.
    - Maps model to the appropriate service via llm_util.get_service_from_model.
    """
    from uniborg import llm_util

    model_in_use = title_model or CHAT_TITLE_MODEL
    service_needed = llm_util.get_service_from_model(model_in_use)
    return model_in_use, service_needed


async def _resolve_title_api_key(
    service_needed: str,
    *,
    api_keys: dict | None = None,
    user_id: int | None = None,
    message_obj=None,
) -> tuple[str | None, int | None]:
    """Resolve API key for the given service.

    Priority:
    1) Explicit api_keys mapping
    2) Finalize user_id (use provided or derive from message_obj)
    3) Lookup API key from llm_db using the finalized user_id

    Returns a tuple of (api_key_or_None, resolved_user_id_or_None).
    """
    from uniborg import llm_db

    # 1) Explicit mapping wins immediately
    mapped_key = (api_keys or {}).get(service_needed)
    if mapped_key:
        return mapped_key, user_id

    # 2) Finalize user_id (prefer provided; otherwise derive from message object)
    resolved_uid = user_id
    if resolved_uid is None and message_obj is not None:
        try:
            resolved_uid = getattr(message_obj, "sender_id", None)
            if resolved_uid is None:
                sender = await message_obj.get_sender()
                resolved_uid = getattr(sender, "id", None)
        except Exception:
            resolved_uid = None

    # 3) Lookup API key if we have a user id
    api_key = None
    if resolved_uid is not None:
        api_key = llm_db.get_api_key(resolved_uid, service=service_needed)

    return api_key, resolved_uid


async def saexec(code: str, **kwargs):
    # Don't clutter locals
    locs = {}
    args = ", ".join(list(kwargs.keys()))
    code_lines = code.split("\n")
    code_lines[-1] = f"return {code_lines[-1]}"
    exec(f"async def func({args}):\n    " + "\n    ".join(code_lines), {}, locs)
    # Don't expect it to return from the coro.
    result = await locs["func"](**kwargs)
    return result


async def clean_cmd(cmd: str):
    return (
        cmd.replace("‘", "'")
        .replace("“", '"')
        .replace("’", "'")
        .replace("”", '"')
        .replace("—", "--")
    )


async def aget(event, command="", shell=True, match=None, album_mode=True):
    if match == None:
        match = event.pattern_match
    if command == "":
        command = await clean_cmd(match.group(2))
        if match.group(1) == "n":
            command = "noglob " + command
    await util.run_and_upload(
        event=event,
        to_await=partial(util.simple_run, command=command, shell=shell),
        album_mode=album_mode,
    )


async def aget_brishz(event, cmd, fork=True, album_mode=True):
    # cmd: an argument array
    ##
    to_await = partial(brishz, cmd=zs("{cmd}"), fork=fork)
    await util.run_and_upload(event=event, to_await=to_await, album_mode=album_mode)


#: The `;` keeps it parsing on a worker that an earlier non-fork command left
#: under `emulate sh` or `setopt ignore_braces`.
BRISH_EVAL_STDIN = '{ eval "$(< /dev/stdin)"; } 2>&1'


@functools.cache
def _lock_takes_cancelled(brish_class) -> bool:
    """Whether `brish_class.acquire_lock` takes `cancelled=` (brish 0.4.1)."""
    try:
        parameters = inspect.signature(brish_class.acquire_lock).parameters
    except (AttributeError, TypeError, ValueError):
        return False
    return "cancelled" in parameters


#: Whether the installed brish lets a wait for a worker be called off (0.4.1
#: and later); a pool of another class is asked on its own.
BRISH_CANCELLED = _lock_takes_cancelled(Brish)


def _on_brish_worker(
    my_brish, *, cwd, server_index, run, may_start=None, cancelled=None, **kwargs
):
    """Runs `run(index)` on one worker of `my_brish`, holding its lock.

    In `cwd`, when given, the worker sets `$jd` to it, changes into it and
    calls `jinit` if defined; it changes back to /tmp afterwards. `may_start`,
    when given, is asked once the worker is ours: when it returns False, the
    lock is released and nothing runs. Returns what `run` returned, or None
    when nothing ran.

    `cancelled`, a callable without arguments, calls off the wait for a
    worker once it returns true, on a brish whose `acquire_lock` takes it
    (0.4.1): brish polls it every 0.05 s from this thread, frees the worker
    and raises BrishCancelledException, and the result is None, as for a
    refused `may_start`. An older brish only has `may_start`, asked once
    the worker is free.

    A worker that is gone (after `exit N` in a non-fork command) fails every
    later call under the same lock with BrishWorkerDiedException; the next
    call after the release gets a working one (brish 0.4.0 restarts the
    pool, 0.4.1 replaces that worker). So the result stands if the command
    ran, and a command that never ran is tried once more.
    """
    lock_kwargs = {}
    if cancelled is not None and _lock_takes_cancelled(type(my_brish)):
        lock_kwargs["cancelled"] = cancelled
    for attempt in range(2):
        try:
            lock, index = my_brish.acquire_lock(
                server_index=server_index, lock_sleep=1, **lock_kwargs
            )
        except BrishCancelledException:
            #: Nothing ran, and brish has released the lock.
            return None
        res = None
        try:
            if may_start is not None and not may_start():
                return None
            if cwd:
                my_brish.z("typeset -g jd={cwd}", server_index=index, **kwargs)
                my_brish.send_cmd(
                    """
                cd "$jd"
                ! ((${+functions[jinit]})) || jinit
                """,
                    server_index=index,
                    **kwargs,
                )

            res = run(index)
            if cwd:
                my_brish.z("cd /tmp", server_index=index, **kwargs)

            return res
        except BrishWorkerDiedException:
            if res is not None:
                return res
            if attempt:
                raise
        finally:
            lock.release()


def _send_on_worker(
    cwd, cmd, *, brish, fork, server_index, may_start, cancelled=None, **kwargs
):
    """`brishz_helper`'s body, in the calling thread; see there."""
    my_brish = plugin_brish() if brish is None else brish

    def run(index):
        return my_brish.send_cmd(
            BRISH_EVAL_STDIN,
            fork=fork,
            cmd_stdin=cmd,
            server_index=index,
            **kwargs,
        )

    return _on_brish_worker(
        my_brish,
        cwd=cwd,
        server_index=server_index,
        run=run,
        may_start=may_start,
        cancelled=cancelled,
        **kwargs,
    )


@force_async
def brishz_helper(
    cwd, cmd, *, brish=None, fork=True, server_index=None, may_start=None, **kwargs
):
    """Runs `cmd` on one worker of `brish`, in `cwd` when given.

    `brish` defaults to the plugin pool (`plugin_brish`); the shell passes
    `persistent_brish`. Returns brish's CmdResult, or None when `may_start`
    refused; see `_on_brish_worker` for a worker that dies.
    """
    return _send_on_worker(
        cwd,
        cmd,
        brish=brish,
        fork=fork,
        server_index=server_index,
        may_start=may_start,
        **kwargs,
    )


@dataclass
class BrishStreamRun:
    """How a streamed brish command ended; its output is in its job."""

    retcode: int


@force_async
def _brishz_job(cwd, cmd, *, brish, fork, server_index, job):
    """Runs `cmd` for `job` on an executor thread, writing its output into it.

    `brish` None is the plugin pool, looked up here: its first use boots its
    workers, which must not block the event loop. With `brish.popen`, the
    output arrives as it comes, and the job's kill hook is the popen's
    `kill`. The loop never breaks on a stop: what a dying command still
    prints arrives, and brish takes its later kill steps inside these reads.
    No brish call happens inside the loop, so BrishWorkerBusyException
    cannot occur. A brish without `popen` runs the command through
    `send_cmd` and writes its whole output at the end. Returns None when the
    job was stopped before the command ran.
    """
    my_brish = plugin_brish() if brish is None else brish
    if not hasattr(my_brish, "popen"):
        res = _send_on_worker(
            cwd,
            cmd,
            brish=my_brish,
            fork=fork,
            server_index=server_index,
            may_start=job.try_start,
            cancelled=job.stop_requested,
        )
        return None if res is None else _write_brish_result(job, res)

    job.output.decoding = shell_stream.Decoding(
        encoding=my_brish.encoding, errors=my_brish.decoding_errors
    )

    def run(index):
        with my_brish.popen(
            BRISH_EVAL_STDIN, fork=fork, cmd_stdin=cmd, server_index=index
        ) as p:
            job.attach(p.kill)
            try:
                for stream, chunk in p:
                    job.output.write(chunk, stream=stream)
            finally:
                job.detach()
        return BrishStreamRun(retcode=p.retcode)

    return _on_brish_worker(
        my_brish,
        cwd=cwd,
        server_index=server_index,
        run=run,
        may_start=job.try_start,
        cancelled=job.stop_requested,
    )


async def brishz_capture(
    *, cwd, cmd, fork=True, job=None, brish=None
) -> typing.Optional[CommandResult]:
    """Runs `cmd` on `brish` (by default the plugin pool) in `cwd` and captures it.

    `fork=False` runs it on server 0 itself, which keeps its state between
    commands (a persistent REPL).

    With a `shell_stream.ShellJob`, the output goes into `job.output` while
    the command runs, and the job can stop it; the result's output is
    `job.output.final_text(render=False)`, the same text as without a job.
    A brish without `popen` runs the command as without a job and writes its
    output into the job at the end; it cannot be stopped once it runs. With
    a job, the result is None when the job was stopped before the command
    ran, and a cancelled await stops the command (StopReason.SHUTDOWN).
    """
    server_index = None
    if fork == False:
        server_index = 0

    if job is None:
        res = await brishz_helper(
            cwd, cmd, brish=brish, fork=fork, server_index=server_index
        )
        return CommandResult(output=res.outerr, retcode=res.retcode)

    try:
        run = await _brishz_job(
            cwd, cmd, brish=brish, fork=fork, server_index=server_index, job=job
        )
    except asyncio.CancelledError:
        job.cancel(reason=shell_stream.StopReason.SHUTDOWN)
        raise
    if run is None:
        return None
    return CommandResult(
        output=job.output.final_text(render=False), retcode=run.retcode
    )


def _write_brish_result(job, res) -> BrishStreamRun:
    """Writes a whole brish result into JOB's output, as if it had streamed.

    The command has ended, so the job is marked ended (`detach`).
    """
    #: Round-trips any text that decoding with `surrogateescape` can give.
    job.output.decoding = shell_stream.Decoding(errors="surrogateescape")
    for stream, text in (
        (shell_stream.STREAM_OUT, res.out),
        (shell_stream.STREAM_ERR, res.err),
    ):
        job.output.write(text.encode("utf-8", "surrogateescape"), stream=stream)
    job.detach()
    return BrishStreamRun(retcode=res.retcode)


async def brishz(event, cwd, cmd, fork=True, shell=True, brish=None, **kwargs):
    """Runs `cmd` like `brishz_capture` and sends its output to the chat."""
    # print(f"entering brishz with cwd: '{cwd}', cmd: '{cmd}'")
    result = await brishz_capture(cwd=cwd, cmd=cmd, fork=fork, brish=brish)
    await send_output(event, result.output, retcode=result.retcode, shell=shell)


def humanbytes(size):
    """Input size in bytes,
    outputs in a human readable format"""
    # https://stackoverflow.com/a/49361727/4723940
    if not size:
        return ""
    # 2 ** 10 = 1024
    power = 2**10
    raised_to_pow = 0
    dict_power_n = {0: "", 1: "Ki", 2: "Mi", 3: "Gi", 4: "Ti"}
    while size > power:
        size /= power
        raised_to_pow += 1
    return str(round(size, 2)) + " " + dict_power_n[raised_to_pow] + "B"


##
def build_menu(buttons, n_cols):
    """Helper to build a menu of inline buttons in a grid."""
    return [buttons[i : i + n_cols] for i in range(0, len(buttons), n_cols)]


##
async def is_group_admin(event) -> bool:
    """Checks if the sender of the event is a group administrator or creator."""
    if not event.is_private:
        chat = await event.get_chat()
        sender = await event.get_sender()
        if chat.megagroup or chat.channel:
            try:
                permissions = await event.client.get_permissions(chat, sender)
                return permissions and (permissions.is_admin or permissions.is_creator)
            except Exception as e:
                print(
                    f"Could not get permissions for {sender.id} in chat {chat.id}: {e}"
                )
                return False
    return False


##
async def async_remove_file(file_path: str):
    """Async file removal with error handling."""
    try:
        await aiofiles.os.remove(file_path)
    except Exception:
        traceback.print_exc()
        pass  # Ignore cleanup errors


async def async_remove_dir(dir_path: str):
    """Async directory removal with error handling."""
    try:
        # Use shutil.rmtree to remove directory and all its contents
        await asyncio.get_event_loop().run_in_executor(
            None, shutil.rmtree, dir_path, True  # ignore_errors=True
        )
    except Exception:
        traceback.print_exc()
        pass  # Ignore cleanup errors


def _generate_random_filename(file_ext: str) -> str:
    """Generate a random filename with the given extension."""
    import uuid

    return f"message_{uuid.uuid4().hex[:8]}{file_ext}"


# Define structured output schema using Pydantic
class FilenameGeneration(BaseModel):
    title: str = Field(description="A clear, descriptive title for the content")
    title_as_file_name: str = Field(
        description="The title formatted as a safe, short filename (alphanumeric, hyphens, underscores only)"
    )
    short_description: str = Field(
        description="A concise summary of the content in maximum 70 words"
    )


#: Takes the (truncated) text and returns its title, file name and summary.
TitleGenerator = Callable[[str], Awaitable[FilenameGeneration]]


async def _default_title_generator(
    *,
    title_model: str | None,
    api_keys: dict | None,
    api_user_id: int | None,
    message_obj,
) -> TitleGenerator | None:
    """`title_model` (or CHAT_TITLE_MODEL) with a resolved key; None without a key."""
    from uniborg import title_util

    model_in_use, service_needed = _get_title_model_and_service(title_model)
    api_key, resolved_uid = await _resolve_title_api_key(
        service_needed,
        api_keys=api_keys,
        user_id=api_user_id,
        message_obj=message_obj,
    )
    if api_user_id is None:
        api_user_id = resolved_uid
    if not api_key:
        print(
            f"Warning: {service_needed} API key not found for user {api_user_id}, falling back to random filename"
        )
        return None

    async def generate(text: str) -> FilenameGeneration:
        return await title_util.complete_structured(
            title_util.FILE_TITLE_PROMPT.format(text=text),
            FilenameGeneration,
            model=model_in_use,
            api_key=api_key,
            api_user_id=api_user_id,
        )

    return generate


async def _generate_file_data(
    text: str,
    parse_mode: str,
    file_name_mode: str,
    *,
    api_user_id: int | None = None,
    api_keys: dict | None = None,
    title_model: str | None = None,
    title_generator: TitleGenerator | None = None,
    message_obj=None,
    default_caption: str | None = None,
) -> FileGeneration:
    """Generate file data (filename, caption, extension) based on the specified mode.

    In "llm" mode, `title_generator` writes the title when given; otherwise
    `title_model` (or CHAT_TITLE_MODEL) does, with a key from `api_keys` or
    the sender's stored keys.
    """
    from uniborg import llm_util

    # Determine file extension based on parse_mode
    file_ext = ".md" if parse_mode == "md" else ".txt"

    if default_caption is None:
        default_caption = (
            "This message is too long, so it has been sent as a text file."
        )

    if file_name_mode == "random":
        filename = _generate_random_filename(file_ext)
        caption = default_caption

    elif file_name_mode == "timestamp":
        from datetime import datetime

        timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        filename = f"message_{timestamp}{file_ext}"
        caption = default_caption

    elif file_name_mode == "llm":
        try:
            if title_generator is None:
                title_generator = await _default_title_generator(
                    title_model=title_model,
                    api_keys=api_keys,
                    api_user_id=api_user_id,
                    message_obj=message_obj,
                )
            if title_generator is None:
                filename = _generate_random_filename(file_ext)
                caption = default_caption
            else:
                truncated_text = llm_util.truncate_text_for_llm(
                    text,
                    mode="start_end",
                    to_length=10000,
                    semantic_boundaries_p=False,
                    start_split=0.6,
                )
                result = await title_generator(truncated_text)
                safe_filename = sanitize_filename(result.title_as_file_name)
                filename = f"{safe_filename}{file_ext}"
                caption = f"**{result.title}**\n\n{result.short_description[:2000]}"

        except Exception as e:
            print(f"Warning: Failed to generate LLM title: {e}")
            traceback.print_exc()
            filename = _generate_random_filename(file_ext)
            caption = f"{default_caption}\n\n(Failed to generate a title.)"
            #: do not put the error in the caption as normal users might see it.

    else:
        filename = _generate_random_filename(file_ext)
        caption = default_caption

    return FileGeneration(filename=filename, caption=caption, extension=file_ext)


async def send_as_file_with_filename(
    *,
    text: str,
    parse_mode: str,
    file_name_mode: str,
    message_obj,
    reply_to=None,
    title_model: str | None = None,
    api_keys: dict | None = None,
    api_user_id: int | None = None,
    title_generator: TitleGenerator | None = None,
    default_caption: str | None = None,
    caption_prefix: str = "",
):
    """Helper function to send text as file with intelligent filename generation.

    `caption_prefix` goes before the generated caption, e.g. an invisible
    marker such as `constants.TWIN_FILE_MARKER`.
    """
    try:
        # Generate file data using shared function, allow it to resolve user_id lazily

        chat = await message_obj.get_chat()
        async with borg.action(chat, "document") as action:
            file_data = await _generate_file_data(
                text,
                parse_mode,
                file_name_mode,
                api_user_id=api_user_id,
                api_keys=api_keys,
                title_model=title_model,
                title_generator=title_generator,
                message_obj=message_obj,
                default_caption=default_caption,
            )

            # Use existing send_text_as_file function
            return await send_text_as_file(
                text=text,
                suffix=file_data.suffix,
                chat=chat,
                caption=f"{caption_prefix}{file_data.caption or ''}",
                reply_to=reply_to,
                filename=file_data.filename,
            )

    except Exception:
        # If file sending fails, silently continue with normal message editing
        pass


##
