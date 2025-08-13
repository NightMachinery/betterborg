# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.

from brish import z, zp, zs, bsh, Brish
from icecream import ic
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
from IPython import embed
import IPython
import sys
import pathlib
from pathlib import Path
import typing
from concurrent.futures import ThreadPoolExecutor
import io
from io import BytesIO
import tempfile

try:
    import PIL
    import PIL.Image
    import PIL.ImageOps
except ImportError:
    PIL = None

import aiofiles


##
def _resize_photo_if_needed(
    file,
    is_image,
    min_width=128,
    min_height=128,
    width=1280,
    height=1280,
    background=(255, 255, 255),
):
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
                if ext == ".gif":
                    # @upstreamBug on GIF files
                    for f in fs:
                        await borg.send_file(chat, f, allow_cache=False, **kwargs)
                else:
                    await borg.send_file(chat, fs, allow_cache=False, **kwargs)
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


async def discreet_send(
    event,
    message,
    reply_to=None,
    quiet=False,
    link_preview=False,
    parse_mode=None,
):
    message = message.strip()
    if quiet or len(message) == 0:
        return reply_to
    else:
        length = len(message)
        last_msg = reply_to
        if length <= 12000:
            s = 0
            e = 4000
            while length > s:
                last_msg = await event.respond(
                    message[s:e],
                    link_preview=link_preview,
                    reply_to=(reply_to if s == 0 else last_msg),
                    parse_mode=parse_mode,
                )
                s = e
                e = s + 4000
        else:
            chat = await event.get_chat()
            last_msg = await send_text_as_file(
                text=message,
                chat=chat,
                reply_to=reply_to,
                caption="This message is too long, so it has been sent as a text file.",
            )
            return last_msg


# Dictionary to track message chains for the edit_message function
# Key: original_message_id, Value: list of child Message objects
EDIT_CHAINS = {}


async def edit_message(
    message_obj, new_text, link_preview=False, parse_mode=None, max_len=4096
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
    """
    global EDIT_CHAINS
    message_id = message_obj.id

    # Sanitize and chunk the new text
    new_text = new_text.strip()
    chunks = (
        [new_text[i : i + max_len] for i in range(0, len(new_text), max_len)]
        if new_text
        else []
    )

    # Get the existing message chain for this message ID
    existing_children = EDIT_CHAINS.get(message_id, [])
    new_children = []

    # Case 1: The new text is empty, delete the entire chain.
    if not chunks:
        for child in existing_children:
            try:
                await child.delete()
            except Exception:
                pass  # Ignore if deletion fails
        EDIT_CHAINS.pop(message_id, None)
        try:
            # Edit the original message to be empty or show a placeholder
            if message_obj.text != "__[empty]__":
                await message_obj.edit("__[empty]__")
        except Exception:
            pass
        return

    # Edit the primary message (the one the user replied to)
    try:
        # --- OPTIMIZATION: Check text before editing ---
        if message_obj.text != chunks[0]:
            await message_obj.edit(
                chunks[0], parse_mode=parse_mode, link_preview=link_preview
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
                    await child_to_edit.edit(new_chunk, parse_mode=parse_mode)

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
            try:
                await child_to_delete.delete()
            except Exception:
                pass  # Ignore deletion errors

    # Update the global state with the new chain configuration
    if new_children:
        EDIT_CHAINS[message_id] = new_children
    else:
        EDIT_CHAINS.pop(message_id, None)


def postproccesor_json(file_path):
    (z("cat {file_path}").out)

    return z("cat {file_path} | command jq . | sponge {file_path}").assert_zero


async def send_text_as_file(
    text: str, *, suffix: str = ".txt", chat, postproccesors=[], **kwargs
):
    f = tempfile.NamedTemporaryFile(suffix=suffix, delete=False)
    try:
        f_path = f.name
        # ic(f_path)

        f.write(text.encode())
        f.close()

        for postproccesor in postproccesors:
            postproccesor(f_path)

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
        await aiofiles.os.removedirs(dir_path)
    except Exception:
        traceback.print_exc()
        pass  # Ignore cleanup errors


##
