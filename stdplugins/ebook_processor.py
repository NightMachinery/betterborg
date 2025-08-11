from telethon import events
from pathlib import Path
from uniborg import util
from brish import zs
import asyncio

# --- New: For handling grouped messages ---
PROCESSED_GROUP_IDS = set()

# --- Configuration ---
# A set of supported ebook file extensions (case-insensitive).
EBOOK_EXTENSIONS = {
    ".epub",
    # ".mobi",
    # ".azw3",
}


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
        func=lambda e: (e.is_private or e.is_group)
        and e.file
        and Path(e.file.name or "").suffix.lower() in EBOOK_EXTENSIONS
    )
)
async def ebook_handler(event):
    """
    Handles incoming messages (including albums) with ebook files from admin
    users, provides user feedback, and processes them as a single request.
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
    status_message = await event.reply("`Processing ebook(s)...`")

    try:
        # The `run_and_upload` utility handles file download, command execution,
        # result upload, and directory cleanup.
        await util.run_and_upload(
            event=event,
            to_await=process_ebooks_and_clean,
            album_mode=True,  # Send cover and .md file(s) together
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
