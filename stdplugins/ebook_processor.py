import asyncio
import io
import os
import shutil
import traceback
from pathlib import Path

import html2text
from telethon import events

import ebooklib
from ebooklib import epub
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

# Maximum size for a single split Markdown file in bytes.
MAX_SPLIT_SIZE = 10 * 1024  # kb


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
        func=lambda e: e.is_private
        and e.file
        and Path(e.file.name or "").suffix.lower() in EBOOK_EXTENSIONS
    )
)
async def ebook_handler(event):
    """
    Handles incoming messages (including albums) with ebook files from admin
    users in private chats only, provides user feedback, and processes them as a
    single request.
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


@borg.on(util.admin_cmd(pattern=r"^\.split$"))
async def split_ebook_handler(event):
    """
    Splits an EPUB file by chapters into multiple Markdown files.
    Triggered by replying `.split` to a message with an EPUB file.
    """
    reply_message = await event.get_reply_message()
    if not reply_message or not reply_message.file:
        status_msg = await event.reply("`Reply to a message with an EPUB file to split it.`")
        return

    if not (reply_message.file.name or "").lower().endswith(".epub"):
        status_msg = await event.reply("`The replied-to file is not an EPUB.`")
        return

    status_msg = await event.reply("`Splitting EPUB by chapters...`")
    temp_dir = Path(f"temp_split_{event.id}")
    temp_dir.mkdir(exist_ok=True)
    downloaded_file_path = None

    try:
        await status_msg.edit("`Downloading EPUB...`")
        downloaded_file_path = await borg.download_media(
            reply_message, file=temp_dir / (reply_message.file.name or "ebook.epub")
        )

        await status_msg.edit("`Parsing EPUB and converting chapters to Markdown...`")
        book = epub.read_epub(downloaded_file_path)
        chapters = []
        h = html2text.HTML2Text()
        h.ignore_links = False
        h.ignore_images = True
        items_by_href = {item.get_name(): item for item in book.get_items()}

        def process_toc_item(toc_item):
            """Recursively processes TOC to extract chapters in order."""
            if isinstance(toc_item, ebooklib.epub.Link):
                href = toc_item.href.split("#")[0]
                if href in items_by_href:
                    item = items_by_href[href]
                    title = toc_item.title
                    content_html = item.get_content().decode("utf-8", "ignore")
                    content_md = h.handle(content_html)
                    chapters.append({"title": title, "content": content_md})
            elif isinstance(toc_item, tuple) and len(toc_item) == 2:
                _, subsections = toc_item
                for sub_item in subsections:
                    process_toc_item(sub_item)

        for toc_item in book.toc:
            process_toc_item(toc_item)

        if not chapters:
            await event.edit(
                "`Could not extract any chapters from the EPUB's Table of Contents.`"
            )
            return

        # Group chapters into files
        await status_msg.edit(
            f"`Grouping {len(chapters)} chapters into chunks smaller than {MAX_SPLIT_SIZE // 1024}KB...`"
        )
        grouped_files = []
        current_group_content = ""
        base_filename = Path(downloaded_file_path).stem
        part_num = 1

        for chapter in chapters:
            chapter_title = chapter["title"]
            chapter_md = f"## {chapter_title}\n\n{chapter['content']}\n\n---\n\n"
            if (
                current_group_content
                and (
                    len(current_group_content.encode("utf-8"))
                    + len(chapter_md.encode("utf-8"))
                )
                > MAX_SPLIT_SIZE
            ):
                grouped_files.append(
                    {
                        "name": f"{base_filename}_part_{part_num}.md",
                        "content": current_group_content,
                    }
                )
                part_num += 1
                current_group_content = ""
            current_group_content += chapter_md

        if current_group_content:
            grouped_files.append(
                {
                    "name": f"{base_filename}_part_{part_num}.md",
                    "content": current_group_content,
                }
            )

        await status_msg.edit(
            f"`Found {len(chapters)} chapters, grouped into {len(grouped_files)} files. Uploading...`"
        )

        for i, group in enumerate(grouped_files):
            file_path = temp_dir / group["name"]
            with open(file_path, "w", encoding="utf-8") as f:
                f.write(group["content"])

            await borg.send_file(
                event.chat_id,
                file_path,
                reply_to=event.id,
                caption=f"Part {i + 1}/{len(grouped_files)} of {base_filename}",
            )
        await status_msg.edit(
            f"`Successfully split and uploaded {len(grouped_files)} parts.`"
        )
        await asyncio.sleep(5)

    except Exception:
        error_message = (
            f"`An error occurred while splitting the EPUB:`\n\n"
            f"```{traceback.format_exc()}```"
        )
        await event.edit(error_message[:4096])
    finally:
        if temp_dir.exists():
            shutil.rmtree(temp_dir)
        if status_msg:
            try:
                await status_msg.delete()
            except Exception:
                pass
