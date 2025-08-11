from telethon import events
from pathlib import Path
from uniborg import util
from uniborg.util import brishz
from brish import zs

# --- Configuration ---
# A set of supported ebook file extensions (case-insensitive).
EBOOK_EXTENSIONS = {
    ".epub",
    # ".mobi",
    # ".azw3",
}


async def process_ebooks_and_clean(cwd, event):
    """
    Finds all ebook files in the given directory, runs conversion commands
    using brish, and then deletes the original files to prevent re-upload.
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

    # Use the brishz utility to execute the full command script.
    await brishz(event=event, cwd=cwd, cmd=full_command, fork=True, shell=True)

    # After successful execution, delete the original ebook files.
    for ebook_file in ebook_files:
        if ebook_file.exists():
            ebook_file.unlink()


@borg.on(
    events.NewMessage(
        func=lambda e: e.is_private
        and e.file
        and Path(e.file.name or "").suffix.lower() in EBOOK_EXTENSIONS
    )
)
async def ebook_handler(event):
    """
    Handles incoming private messages (including forwards) with ebook files
    from admin users only.
    """
    # Restrict this command to admins.
    if not await util.isAdmin(event):
        return

    # The `run_and_upload` utility handles file download, command execution,
    # result upload, and cleanup. We pass our custom processing function
    # to it via the `to_await` argument.
    await util.run_and_upload(
        event=event,
        to_await=process_ebooks_and_clean,
        album_mode=True,  # Send cover and .md file(s) together
    )
