# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.

from uniborg.constants import (
    BOT_META_INFO_PREFIX,
    DEFAULT_FILE_LENGTH_THRESHOLD,
    DEFAULT_FILE_ONLY_LENGTH_THRESHOLD,
    CHAT_TITLE_MODEL,
)
from pynight.common_files import sanitize_filename
import json
from pydantic import BaseModel, Field
import litellm
from brish import z, zp, zs, bsh, Brish
from pynight.common_icecream import ic
from collections.abc import Iterable
from IPython.terminal.embed import InteractiveShellEmbed, InteractiveShell
from IPython.terminal.ipapp import load_default_config
from aioify import aioify
import functools
from functools import partial
import uuid
import asyncio
import subprocess
import traceback
import os
import pexpect
import re
import itertools
import shutil
from uniborg import util
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
executor = ThreadPoolExecutor(max_workers=(brish_count + 16))


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


def init_brishes():
    print(f"Initializing {brish_count} brishes ...")
    global persistent_brish

    executor.submit(lambda: brish_server_cleanup(persistent_brish))

    boot_cmd = "export JBRISH=y ; unset FORCE_INTERACTIVE"
    persistent_brish = Brish(boot_cmd=boot_cmd, server_count=brish_count)
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
    )


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
        sender = getattr(msg, "sender", None) if msg else None
        sender = sender or getattr(event, "sender", None)

        sender_id = getattr(sender, "id", None) or getattr(event, "sender_id", None)
        sender_username = getattr(sender, "username", None)
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

            if chat:
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
):
    if cwd is None:
        cwd = dl_base + str(uuid.uuid4()) + "/"
    Path(cwd).mkdir(parents=True, exist_ok=True)
    a = borg
    dled_files = []

    async def dl(z):
        if z is not None and getattr(z, "file", None) is not None:
            dled_file_name = getattr(z.file, "name", "")
            dled_file_name = dled_file_name or f"some_file_{uuid.uuid4().hex}"
            dled_path = f"{cwd}{z.id}_{dled_file_name}"
            dled_path = await a.download_media(message=z, file=dled_path)
            mdate = os.path.getmtime(dled_path)
            dled_files.append((dled_path, mdate, dled_file_name))

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


async def handle_exc(event, reply_exc=True):
    #: `reply_exc` should be False for bots facing random users (as opposed to admins).
    ##
    exc = "Julia encountered an exception. :(\n" + traceback.format_exc()
    await send_output(event, exc, shell=(reply_exc), retcode=1)


async def handle_exc_chat(chat, reply_exc=True):
    # @todo2 refactor send_output to work with just a chat, not an event
    exc = "Julia encountered an exception. :(\n" + traceback.format_exc()
    await borg.send_message(chat, exc)


async def send_files(chat, files, **kwargs):
    if isinstance(files, str) or not isinstance(files, Iterable):
        try:
            await borg.send_file(chat, files, allow_cache=False, **kwargs)
        except:
            await handle_exc_chat(chat)
        return

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
                        await borg.send_file(chat, fs, allow_cache=False, **kwargs)
                    except PhotoExtInvalidError:
                        print(
                            f"Album sending failed, using no-album workaround. Files: {fs}"
                        )
                        use_no_album = True

                if use_no_album:
                    for f in fs:
                        await borg.send_file(chat, f, allow_cache=False, **kwargs)
            except:
                await handle_exc_chat(chat)


async def run_and_upload(event, to_await, quiet=True, reply_exc=True, album_mode=True):
    file_add = ""
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
        # client = borg
        files = list(Path(cwd).glob("*"))
        if album_mode and len(files) != 1:
            files = [p.absolute() for p in files if not p.is_dir()]
            await send_files(chat, files)
        else:
            files.sort()
            for p in files:
                if (
                    not p.is_dir()
                ):  # and not any(s in p.name for s in ('.torrent', '.aria2')):
                    file_add = p.absolute()
                    base_name = str(await os_aio.path.basename(file_add))
                    # trying_to_upload_msg = await util.discreet_send(
                    # event, "Julia is trying to upload \"" + base_name +
                    # "\".\nPlease wait ...", trying_to_dl, quiet)
                    voice_note = base_name.startswith("voicenote-")
                    video_note = base_name.startswith("videonote-")
                    force_doc = base_name.startswith("fdoc-")
                    supports_streaming = base_name.startswith("streaming-")
                    if False:
                        att, mime = telethon.utils.get_attributes(file_add)
                        print(f"File attributes: {att.__dict__}")
                    async with borg.action(chat, "document") as action:
                        try:
                            await borg.send_file(
                                chat,
                                file_add,
                                voice_note=voice_note,
                                video_note=video_note,
                                supports_streaming=supports_streaming,
                                force_document=force_doc,
                                reply_to=event.message,
                                allow_cache=False,
                            )
                            #                            progress_callback=action.progress)
                            # caption=base_name)
                        except:
                            await handle_exc(event, reply_exc)
    except:
        await handle_exc(event, reply_exc)
    finally:
        await remove_potential_file(cwd, event)


async def safe_run(event, cwd, command):
    # await event.reply('bash -c "' + command + '"' + '\n' + cwd)
    # await pexpect_ai.run(command, cwd=cwd)
    await subprocess_aio.run(command, cwd=cwd)


async def simple_run(event, cwd, command, shell=True):
    sp = await subprocess_aio.run(
        command,
        shell=shell,
        cwd=cwd,
        text=True,
        executable="zsh" if shell else None,
        stderr=subprocess.STDOUT,
        stdout=subprocess.PIPE,
    )
    output = sp.stdout
    await send_output(event, output, retcode=sp.returncode, shell=shell)


async def send_output(event, output: str, retcode=-1, shell=True):
    output = output.strip()
    output = f"The process exited {retcode}." if output == "" else output
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


def _find_best_split_point(
    text: str,
    start_pos: int,
    max_length: int,
    *,
    search_direction: int = -1,
    buffer_size=600,
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


@dataclass
class EditChainState:
    """Stores the state of an edit chain including children and last computed text."""

    children: list = None
    last_text: str = ""

    def __post_init__(self):
        if self.children is None:
            self.children = []


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


async def _cleanup_message_chain(edit_state, message_id):
    """Clean up an existing message chain by deleting all child messages."""
    existing_children = edit_state.children

    # Delete all child messages
    for child in existing_children:
        await _safe_delete_message(child)

    # Remove from edit chains tracking
    EDIT_CHAINS.pop(message_id, None)


def _log_file_sending_error(context_name):
    """Log file sending error in a standardized way."""
    print(f"File sending failed in {context_name}:", file=sys.stderr)
    traceback.print_exc()


# Dictionary to track message chains for the edit_message function
# Key: original_message_id, Value: EditChainState object
EDIT_CHAINS = {}


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
):
    """
    Intelligently edits a message chain to reflect new text content,
    avoiding redundant API calls.

    - Compares text content before sending an edit request.
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
        reply_to: Optional message to reply to when sending a file.
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
    """
    global EDIT_CHAINS
    message_id = message_obj.id

    new_text = new_text.strip()

    # Get or create the edit state for this message ID
    edit_state = EDIT_CHAINS.get(message_id, EditChainState())

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

    # If we should skip text editing, clean up message chain and send file
    if only_send_file:
        # Clean up existing message chain since we're only sending file
        edit_state = EDIT_CHAINS.get(message_id, EditChainState())
        await _cleanup_message_chain(edit_state, message_id)

        # Clear the original message or replace with placeholder
        try:
            await message_obj.edit(
                "__[sent as file]__",
                parse_mode="md",
            )
        except Exception:
            pass

        # Send the file
        try:
            await _send_as_file_with_filename(
                text=new_text,
                parse_mode=parse_mode,
                file_name_mode=file_name_mode,
                message_obj=message_obj,
                reply_to=reply_to,
                title_model=title_model,
                api_keys=api_keys,
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
            for child in existing_children:
                await _safe_delete_message(child)
            EDIT_CHAINS.pop(message_id, None)
            try:
                # Edit the original message to be empty or show a placeholder
                if message_obj.text != "__[empty]__":
                    await message_obj.edit(
                        "__[empty]__",
                        parse_mode="md",
                    )
            except Exception:
                pass
            return

        # Edit the primary message (the one the user replied to)
        try:
            # --- OPTIMIZATION: Check text before editing ---
            if message_obj.text != chunks[0]:
                await message_obj.edit(
                    chunks[0],
                    parse_mode=parse_mode,
                    link_preview=link_preview,
                )
        except telethon.errors.rpcerrorlist.MessageNotModifiedError:
            pass  # Fallback for safety, though the check above should prevent this.
        except Exception as e:
            print(f"Error editing original message {message_id}: {e}")
            return  # If the head of the chain fails, abort

        # Now, handle the children (the rest of the chunks)
        num_new_chunks = len(chunks) - 1
        num_existing_children = len(existing_children)
        last_message_in_chain = message_obj

        for i in range(max(num_new_chunks, num_existing_children)):
            # --- Edit existing messages if we have a chunk for them ---
            if i < num_new_chunks and i < num_existing_children:
                child_to_edit = existing_children[i]
                new_chunk = chunks[i + 1]
                try:
                    # --- OPTIMIZATION: Check text before editing ---
                    if child_to_edit.text != new_chunk:
                        await child_to_edit.edit(
                            new_chunk,
                            parse_mode=parse_mode,
                            link_preview=link_preview,
                        )

                    new_children.append(child_to_edit)
                    last_message_in_chain = child_to_edit
                except telethon.errors.rpcerrorlist.MessageNotModifiedError:
                    # This is a fallback, but the check above should prevent it.
                    new_children.append(child_to_edit)
                    last_message_in_chain = child_to_edit
                except Exception:
                    # If editing a child fails, stop processing the chain to avoid errors.
                    break

            # --- Create new messages if new text is longer ---
            elif i < num_new_chunks:
                try:
                    new_child = await last_message_in_chain.reply(
                        chunks[i + 1], parse_mode=parse_mode
                    )
                    new_children.append(new_child)
                    last_message_in_chain = new_child
                except Exception:
                    break  # Stop if we can't send a new reply

            # --- Delete surplus messages if new text is shorter ---
            elif i < num_existing_children:
                child_to_delete = existing_children[i]
                await _safe_delete_message(child_to_delete)

        # Update the global state with the new chain configuration
        edit_state.children = new_children
        edit_state.last_text = (
            new_text  # Store the last text for future append operations
        )

        if new_children or new_text:
            EDIT_CHAINS[message_id] = edit_state
        else:
            EDIT_CHAINS.pop(message_id, None)

    finally:
        # Send file after message editing (success or failure)
        if decision.send_file and decision.send_text:
            try:
                await _send_as_file_with_filename(
                    text=new_text,
                    parse_mode=parse_mode,
                    file_name_mode=file_name_mode,
                    message_obj=message_obj,
                    reply_to=reply_to,
                    title_model=title_model,
                    api_keys=api_keys,
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


@force_async
def brishz_helper(myBrish, cwd, cmd, fork=True, server_index=None, **kwargs):
    lock, server_index = myBrish.acquire_lock(server_index=server_index, lock_sleep=1)
    try:
        if cwd:
            myBrish.z("typeset -g jd={cwd}", server_index=server_index, **kwargs)
            myBrish.send_cmd(
                """
            cd "$jd"
            ! ((${+functions[jinit]})) || jinit
            """,
                server_index=server_index,
                **kwargs,
            )

        res = myBrish.send_cmd(
            '{ eval "$(< /dev/stdin)" } 2>&1',
            fork=fork,
            cmd_stdin=cmd,
            server_index=server_index,
            **kwargs,
        )
        if cwd:
            myBrish.z("cd /tmp", server_index=server_index, **kwargs)

        return res
    finally:
        lock.release()


async def brishz(event, cwd, cmd, fork=True, shell=True, **kwargs):
    # print(f"entering brishz with cwd: '{cwd}', cmd: '{cmd}'")
    res = None
    server_index = None
    if fork == False:
        server_index = 0  # to have a persistent REPL

    res = await brishz_helper(
        persistent_brish, cwd, cmd, fork=fork, server_index=server_index
    )

    await send_output(event, res.outerr, retcode=res.retcode, shell=shell)


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


async def _generate_file_data(
    text: str,
    parse_mode: str,
    file_name_mode: str,
    *,
    api_user_id: int | None = None,
    api_keys: dict | None = None,
    title_model: str | None = None,
    message_obj=None,
) -> FileGeneration:
    """Generate file data (filename, caption, extension) based on the specified mode."""
    # Determine file extension based on parse_mode
    file_ext = ".md" if parse_mode == "md" else ".txt"
    default_caption = "This message is too long, so it has been sent as a text file."

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

            # Decide which service is needed based on the model in use
            model_in_use, service_needed = _get_title_model_and_service(title_model)

            # Resolve API key using shared helper
            api_key_to_use, resolved_uid = await _resolve_title_api_key(
                service_needed,
                api_keys=api_keys,
                user_id=api_user_id,
                message_obj=message_obj,
            )
            if api_user_id is None:
                api_user_id = resolved_uid

            if not api_key_to_use:
                print(
                    f"Warning: {service_needed} API key not found for user {api_user_id}, falling back to random filename"
                )
                filename = _generate_random_filename(file_ext)
                caption = default_caption
            else:
                # Set up LiteLLM with the API key
                # Use LiteLLM with structured output
                response = litellm.completion(
                    api_key=api_key_to_use,
                    model=model_in_use,
                    messages=[
                        {
                            "role": "user",
                            "content": f"Generate a title and filename for this text content:\n\n{text[:10000]}...",
                        }
                    ],
                    response_format=FilenameGeneration,
                )

                # Parse the structured response using Pydantic
                result = FilenameGeneration.model_validate_json(
                    response.choices[0].message.content
                )
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


async def _send_as_file_with_filename(
    *,
    text: str,
    parse_mode: str,
    file_name_mode: str,
    message_obj,
    reply_to=None,
    title_model: str | None = None,
    api_keys: dict | None = None,
    api_user_id: int | None = None,
):
    """Helper function to send text as file with intelligent filename generation."""
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
                message_obj=message_obj,
            )

            # Use existing send_text_as_file function
            await send_text_as_file(
                text=text,
                suffix=file_data.suffix,
                chat=chat,
                caption=file_data.caption,
                reply_to=reply_to,
                filename=file_data.filename,
            )

    except Exception:
        # If file sending fails, silently continue with normal message editing
        pass


##
