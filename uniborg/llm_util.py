import traceback
from uniborg import util
from uniborg import llm_db
from uniborg.constants import BOT_META_INFO_PREFIX
import llm
from pathlib import Path
import os
import uuid


# --- LLM-Specific Shared Constants and Utilities ---

MIME_TYPE_MAP = {
    ##
    #: Audio formats
    ".ogg": "audio/ogg",
    ".oga": "audio/ogg",
    ".m4a": "audio/aac",
    # ".mp3": "audio/mpeg",
    # ".wav": "audio/wav",
    # ".flac": "audio/flac",
    ##
    #: Video formats
    ".mp4": "video/mp4",
    # ".mov": "video/quicktime",
    # ".webm": "video/webm",
    # ".mkv": "video/x-matroska",
    ##
}


def create_attachments_from_dir(directory: Path) -> list[llm.Attachment]:
    """
    Scans a directory for files and creates a list of llm.Attachment objects.

    It uses a predefined MIME_TYPE_MAP to handle common audio/video types
    by reading their binary content directly. For all other file types (e.g.,
    images), it creates an attachment by path, allowing the `llm` library
    to infer the type.

    Args:
        directory: The Path object of the directory to scan.

    Returns:
        A list of llm.Attachment objects, ready to be sent to a model.
    """
    attachments = []
    if not directory.is_dir():
        return attachments

    for filepath in directory.iterdir():
        if not filepath.is_file():
            continue

        lower_filename = filepath.name.lower()
        handled_explicitly = False

        for extension, mime_type in MIME_TYPE_MAP.items():
            if lower_filename.endswith(extension):
                try:
                    with open(filepath, "rb") as f:
                        content = f.read()
                    attachments.append(llm.Attachment(content=content, type=mime_type))
                    handled_explicitly = True
                    break  # Move to the next file
                except IOError as e:
                    print(f"Error reading file {filepath} for explicit MIME type: {e}")
                    # Mark as handled to prevent fallback, but skip adding it
                    handled_explicitly = True
                    break

        if not handled_explicitly:
            # Fallback for images and other file types.
            # llm.Attachment(path=...) is smart enough to handle these.
            attachments.append(llm.Attachment(path=str(filepath)))

    return attachments


async def handle_llm_error(
    *,
    event,
    exception,
    response_message=None,
    service: str = None,
    base_error_message: str = None,
    error_id_p: bool = True,
):
    """A generic error handler for LLM related operations."""
    error_id = uuid.uuid4() if error_id_p else None
    error_message = str(exception)

    if base_error_message:
        base_user_facing_error = f"{BOT_META_INFO_PREFIX}{base_error_message}"
    else:
        base_user_facing_error = f"{BOT_META_INFO_PREFIX}An error occurred."

    user_facing_error = (
        f"{base_user_facing_error} (Error ID: `{error_id}`)"
        if error_id
        else base_user_facing_error
    )

    should_show_error_to_user = False
    if "quota" in error_message.lower() or "exceeded" in error_message.lower():
        should_show_error_to_user = True
    elif service and "api key not valid" in error_message.lower():
        # Special handling for invalid API key
        if response_message:
            try:
                await response_message.delete()
            except Exception as delete_e:
                print(f"Error deleting message: {delete_e}")
        await llm_db.request_api_key_message(event, service)
        if error_id:
            print(f"--- ERROR ID: {error_id} ---")
        traceback.print_exc()
        return

    is_admin = await util.isAdmin(event)
    is_private = event.is_private
    if is_private and is_admin:
        should_show_error_to_user = True

    if should_show_error_to_user:
        user_facing_error = f"{user_facing_error}\n\n**Error:** {error_message}"

    try:
        if response_message:
            await response_message.edit(user_facing_error)
        else:
            await event.reply(user_facing_error)
    except Exception as e:
        print(f"Error while sending/editing error message: {e}")

    if error_id:
        print(f"--- ERROR ID: {error_id} ---")
    traceback.print_exc()


def create_llm_start_handler(service: str, welcome_message: str, configured_message: str):
    """
    Creates a generic /start command handler for onboarding and LLM API key checks.
    """

    async def start_handler(event):
        # Cancel any pending input flows from other modules
        if llm_db.is_awaiting_key(event.sender_id):
            llm_db.cancel_key_flow(event.sender_id)

        # Check if the required API key is configured
        if llm_db.get_api_key(user_id=event.sender_id, service=service):
            await event.reply(configured_message)
        else:
            # If not configured, start the key request flow
            await llm_db.request_api_key_message(event, service)

    return start_handler
