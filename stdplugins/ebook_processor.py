import asyncio
import io
import os
import shutil
import traceback
from pathlib import Path

from telethon import events
from uniborg import util, epub_util
from brish import zs

# --- New: For handling grouped messages ---
PROCESSED_GROUP_IDS = set()

# --- Configuration ---
# A set of supported ebook file extensions (case-insensitive).
EBOOK_EXTENSIONS = {
    ".epub",
    # ".mobi",
    # ".azw3",
}

# AUTO_PROCESS_MODE configuration
# "PV": only processes books sent in private chats
# dict: a mapping of chat names to chat IDs - only process books sent in those chat IDs
AUTO_PROCESS_MODE = {
    "Books": -1001304139500,
}


def should_auto_process(event):
    """
    Determines if an ebook should be auto-processed based on AUTO_PROCESS_MODE.

    Returns:
        bool: True if the event should trigger auto-processing, False otherwise.
    """
    if AUTO_PROCESS_MODE == "PV":
        return event.is_private
    elif isinstance(AUTO_PROCESS_MODE, dict):
        return event.chat_id in AUTO_PROCESS_MODE.values()
    else:
        print(f"Invalid AUTO_PROCESS_MODE configuration: '{AUTO_PROCESS_MODE}'")
        return False


async def process_ebooks_and_clean(cwd, event):
    """
    Finds all ebook files in the given directory, runs conversion commands,
    and then deletes the original files only upon success.
    This function is designed to be called by `util.run_and_upload`.
    """
    ebook_files = [
        f
        for f in Path(cwd).iterdir()
        if f.is_file() and f.suffix.lower() in EBOOK_EXTENSIONS
    ]

    if not ebook_files:
        await event.reply("Error: Could not find any supported ebook files to process.")
        return

    # Build a single, multi-part command string to process all files in one go.
    command_parts = []
    for ebook_file in ebook_files:
        # We can use relative paths since the command runs inside `cwd`.
        filename = ebook_file.name
        md_filename = ebook_file.with_suffix(".md").name

        # Use zs() to safely construct each part of the command.
        command_parts.append(zs("ebook-cover {filename}"))
        command_parts.append(zs("epub2md {filename} > {md_filename}"))

    full_command = " && ".join(command_parts)

    # Directly use the brishz_helper to get the command result, which allows
    # us to control the output sent back to the user.
    res = await util.brishz_helper(util.persistent_brish, cwd, full_command, fork=True)

    if res.retcode == 0:
        # On success, delete the original ebook files to prevent re-upload.
        for ebook_file in ebook_files:
            if ebook_file.exists():
                ebook_file.unlink()
    else:
        # On failure, send the error output and do not delete the originals.
        await util.send_output(event, res.outerr, retcode=res.retcode)


@borg.on(
    events.NewMessage(
        func=lambda e: should_auto_process(e)
        and e.file
        and Path(e.file.name or "").suffix.lower() in EBOOK_EXTENSIONS
    )
)
async def ebook_handler(event):
    """
    Handles incoming messages (including albums) with ebook files from admin
    users in configured chats (based on AUTO_PROCESS_MODE), provides user feedback,
    and processes them as a single request.
    """
    # Restrict this command to admins.
    if not await util.isAdmin(event):
        return

    # --- Grouped message handling ---
    group_id = event.grouped_id
    if group_id:
        if group_id in PROCESSED_GROUP_IDS:
            return  # This album is already being processed

        PROCESSED_GROUP_IDS.add(group_id)

    # --- Status message handling ---
    status_message = await event.reply("Processing ebook(s)…")

    try:
        # The `run_and_upload` utility handles file download, command execution,
        # result upload, and directory cleanup.
        await util.run_and_upload(
            event=event,
            to_await=process_ebooks_and_clean,
            album_mode=False,  #: It's better to send each book separately, not grouped together
            quiet=True,  # Suppress default status messages from run_and_upload
        )
    finally:
        # --- Cleanup ---
        if status_message:
            try:
                await status_message.delete()
            except Exception:
                pass

        if group_id:
            # Give a bit more time before allowing the same group to be processed again
            await asyncio.sleep(10)
            PROCESSED_GROUP_IDS.discard(group_id)


async def split_ebook_and_clean(cwd, event):
    """
    Finds the ebook file in the directory, chunks it using the advanced
    epub_util, saves the chunks as text files, and deletes the original.
    """
    ebook_files = [
        f
        for f in Path(cwd).iterdir()
        if f.is_file() and f.suffix.lower() in EBOOK_EXTENSIONS
    ]

    if not ebook_files:
        await event.reply("Error: Could not find a supported ebook file to split.")
        return

    if len(ebook_files) > 1:
        await event.reply(
            "Warning: Multiple EPUBs found. Splitting only the first one."
        )

    ebook_file = ebook_files[0]

    try:
        # Use the new, sophisticated chunking utility
        chunks = epub_util.chunk_epub(str(ebook_file))

        if not chunks:
            await event.reply("Could not extract any text from the EPUB.")
            return

        book_name = ebook_file.stem

        # Save chunks to sequentially named text files
        for i, chunk in enumerate(chunks):
            chunk_filename = Path(cwd) / f"{book_name}_part_{i+1:03d}.txt"
            with open(chunk_filename, "w", encoding="utf-8") as f:
                f.write(chunk)

    except Exception as e:
        await event.reply(f"An error occurred during EPUB chunking: {e}")
    finally:
        # Delete original ebook files to prevent re-upload
        for f in ebook_files:
            if f.exists():
                f.unlink()


@borg.on(util.admin_cmd(pattern=r"^\.split$"))
async def split_ebook_handler(event):
    """
    Handles the .split command for an EPUB file. Chunks the EPUB's
    text content into multiple text files using structural and semantic splitting.
    """
    replied_msg = await event.get_reply_message()
    has_epub = (
        event.file and Path(event.file.name or "").suffix.lower() in EBOOK_EXTENSIONS
    )
    reply_has_epub = (
        replied_msg
        and replied_msg.file
        and Path(replied_msg.file.name or "").suffix.lower() in EBOOK_EXTENSIONS
    )

    if not has_epub and not reply_has_epub:
        await event.reply(
            "Please reply to a message with an EPUB file or send one with the `.split` command."
        )
        return

    status_message = None
    try:
        # A bot cannot edit a user's message. It must send a new one.
        status_message = await event.reply("Splitting EPUB into text chunks...")

        await util.run_and_upload(
            event=event,
            to_await=split_ebook_and_clean,
            quiet=True,
            album_mode=True,  # Upload all generated .txt files together
        )
    finally:
        # Clean up the bot's status message
        if status_message:
            try:
                await status_message.delete()
            except Exception:
                pass
        # Try to delete the user's original command (will only work for userbots)
        try:
            await event.delete()
        except Exception:
            pass
