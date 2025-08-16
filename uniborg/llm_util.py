from pynight.common_icecream import ic
import traceback
from uniborg import util
from uniborg import llm_db
from uniborg.constants import BOT_META_INFO_PREFIX
import llm
from pathlib import Path
import os
import uuid


# --- Custom Exceptions ---


class TelegramUserReplyException(Exception):
    """Base exception for errors that should be sent directly to the user as diagnostic messages."""

    pass


class ProxyRestrictedException(TelegramUserReplyException):
    """Exception raised when proxy access is restricted for non-admin users."""

    pass


# --- Proxy Configuration ---

GEMINI_SPECIAL_HTTP_PROXY_ADMIN_ONLY_P = os.getenv(
    "GEMINI_SPECIAL_HTTP_PROXY_ADMIN_ONLY_P", "y"
).lower() in ("true", "y")


def get_proxy_config_or_error(user_id: int) -> tuple[str | None, str | None]:
    """Get proxy configuration or return error message if blocked.

    Returns:
        tuple: (proxy_url, error_message) - one will be None
    """
    proxy_url = os.getenv("GEMINI_SPECIAL_HTTP_PROXY")
    if not proxy_url:
        return None, None

    if GEMINI_SPECIAL_HTTP_PROXY_ADMIN_ONLY_P and not util.is_admin_by_id(user_id):
        raise ProxyRestrictedException(
            "🚫 This Gemini feature is currently unavailable due to regional restrictions. "
            "Our servers operate within EU jurisdiction where certain advanced AI capabilities "
            "require special compliance measures. Please try again later or contact support."
        )

    return proxy_url, None


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


async def _handle_common_error_cases(
    *,
    event,
    exception,
    response_message=None,
    service: str = None,
    error_id: str = None,
) -> bool:
    """Handle common error cases that apply to both LLM and TTS errors.

    Returns:
        bool: True if the error was handled and processing should stop, False otherwise
    """
    error_message = f"{BOT_META_INFO_PREFIX}{str(exception)}"

    if isinstance(exception, TelegramUserReplyException):
        try:
            if response_message:
                await response_message.edit(error_message)
            else:
                await event.reply(error_message)
        except Exception as e:
            print(f"Error while sending/editing user reply exception message: {e}")
        return True

    # Special handling for invalid API key
    if service and "api key not valid" in error_message.lower():
        if response_message:
            try:
                await response_message.delete()
            except Exception as delete_e:
                print(f"Error deleting message: {delete_e}")
        await llm_db.request_api_key_message(event, service)
        # if error_id:
        #     print(f"--- ERROR ID: {error_id} ---")
        # traceback.print_exc()
        return True

    return False


def _should_show_error_details(
    error_message: str, is_admin: bool, is_private: bool
) -> bool:
    """Determine if error details should be shown to the user."""
    if "quota" in error_message.lower() or "exceeded" in error_message.lower():
        return True
    if is_private and is_admin:
        return True
    return False


async def handle_error(
    *,
    event,
    exception,
    error_type: str = "LLM",
    response_message=None,
    service: str = None,
    base_error_message: str = None,
    error_id_p: bool = True,
):
    """A unified error handler for LLM and TTS related operations."""
    error_id = uuid.uuid4() if error_id_p else None
    error_message = str(exception)

    # Handle common error cases
    if await _handle_common_error_cases(
        event=event,
        exception=exception,
        response_message=response_message,
        service=service,
        error_id=str(error_id) if error_id else None,
    ):
        return

    # Determine default error message based on type
    if base_error_message:
        base_user_facing_error = f"{BOT_META_INFO_PREFIX}{base_error_message}"
    elif error_type.upper() == "TTS":
        base_user_facing_error = f"{BOT_META_INFO_PREFIX}TTS generation failed."
    else:
        base_user_facing_error = f"{BOT_META_INFO_PREFIX}An error occurred."

    user_facing_error = (
        f"{base_user_facing_error} (Error ID: `{error_id}`)"
        if error_id
        else base_user_facing_error
    )

    is_admin = await util.isAdmin(event)
    is_private = event.is_private

    if _should_show_error_details(error_message, is_admin, is_private):
        user_facing_error = f"{user_facing_error}\n\n**Error:** {error_message}"

    try:
        if response_message:
            await response_message.edit(user_facing_error)
        else:
            await event.reply(user_facing_error)
    except Exception as e:
        print(f"Error while sending/editing error message: {e}")

    if error_id:
        error_type_prefix = (
            f"{error_type.upper()} " if error_type.upper() != "LLM" else ""
        )
        print(f"--- {error_type_prefix}ERROR ID: {error_id} ---")
    traceback.print_exc()


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
    await handle_error(
        event=event,
        exception=exception,
        error_type="LLM",
        response_message=response_message,
        service=service,
        base_error_message=base_error_message,
        error_id_p=error_id_p,
    )


def create_llm_start_handler(
    service: str, welcome_message: str, configured_message: str
):
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
