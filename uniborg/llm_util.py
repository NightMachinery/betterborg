from pynight.common_icecream import ic
import traceback
from uniborg import util
from uniborg import llm_db
from uniborg.constants import BOT_META_INFO_PREFIX
import llm
from pathlib import Path
import os
import re
import uuid
import json


# --- Custom Exceptions ---


class TelegramUserReplyException(Exception):
    """Base exception for errors that should be sent directly to the user as diagnostic messages."""

    pass


class RateLimitException(TelegramUserReplyException):
    """Exception raised for API rate limit errors, containing the original exception."""

    def __init__(self, message, original_exception=None):
        super().__init__(message)
        self.original_exception = original_exception


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


def h_extract_exception_json_safely(text):
    # Find b'{ start
    b_start = text.find("b'{")
    if b_start == -1:
        return None

    # Get content after b'
    remaining = text[b_start + 2 :]  # Skip "b'"

    # Find the last quote (end of the b'...' block)
    last_quote = remaining.rfind("'")
    if last_quote == -1:
        return None

    # Extract and clean the raw JSON
    raw_json = remaining[:last_quote]
    clean_json = raw_json.replace("\\n", "\n")

    # Find actual JSON end by counting braces
    brace_count = 0
    json_end = -1
    for i, char in enumerate(clean_json):
        if char == "{":
            brace_count += 1
        elif char == "}":
            brace_count -= 1
            if brace_count == 0:
                json_end = i + 1
                break

    if json_end > 0:
        valid_json = clean_json[:json_end]
        return json.loads(valid_json)

    return None


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

    # New block for RateLimitException
    if isinstance(exception, RateLimitException):
        error_message = f"{BOT_META_INFO_PREFIX}🚫 **API Rate Limit Exceeded**\n\n"
        original_msg = str(getattr(exception, "original_exception", ""))

        # Extract JSON from the exception if present
        error_data = h_extract_exception_json_safely(original_msg)
        if error_data:
            try:
                # Extract main error message
                if "error" in error_data and "message" in error_data["error"]:
                    error_message += (
                        f"**Details:** {error_data['error']['message']}\n\n"
                    )

                # Extract quota information
                if "error" in error_data and "details" in error_data["error"]:
                    for detail in error_data["error"]["details"]:
                        if (
                            detail.get("@type")
                            == "type.googleapis.com/google.rpc.QuotaFailure"
                        ):
                            violations = detail.get("violations", [])
                            for violation in violations:
                                quota_metric = violation.get("quotaMetric", "").split(
                                    "/"
                                )[-1]
                                quota_id = violation.get("quotaId", "")
                                quota_value = violation.get("quotaValue", "")
                                model = violation.get("quotaDimensions", {}).get(
                                    "model", ""
                                )

                                if quota_metric and quota_value:
                                    error_message += f"**Quota exceeded:** {quota_metric.replace('_', ' ').title()}\n"
                                    error_message += (
                                        f"**Limit:** {quota_value} tokens per minute\n"
                                    )
                                    if model:
                                        error_message += f"**Model:** {model}\n"
                                    error_message += "\n"

                        elif (
                            detail.get("@type")
                            == "type.googleapis.com/google.rpc.RetryInfo"
                        ):
                            retry_delay = detail.get("retryDelay", "")
                            if retry_delay:
                                error_message += (
                                    f"**Suggested wait time:** `{retry_delay}`\n\n"
                                )

                        elif (
                            detail.get("@type") == "type.googleapis.com/google.rpc.Help"
                        ):
                            links = detail.get("links", [])
                            for link in links:
                                if "url" in link:
                                    error_message += f"**More info:** {link['url']}\n\n"

                # Show full JSON for admins
                is_admin = await util.isAdmin(event)
                if is_admin:
                    formatted_json = json.dumps(error_data, indent=2)
                    error_message += f"**Full error details (admin only):**\n```json\n{formatted_json}\n```"

            except (json.JSONDecodeError, KeyError) as parse_error:
                print(f"Error parsing rate limit JSON: {parse_error}")
                # Fallback to simple message extraction
                # match = re.search(r'"message":\s*"([^"]+)"', original_msg)
                # if match:
                #     error_message += f"**Details:** {match.group(1)}\n\n"
                # else:
                #     error_message += "You have sent too many requests in a short period. Please wait and try again.\n\n"

                delay_match = re.search(r'"retryDelay":\s*"([^"]+)"', original_msg)
                if delay_match:
                    error_message += (
                        f"**Suggested wait time:** `{delay_match.group(1)}`"
                    )
        else:
            # Fallback if no JSON found - put whole message in code block for admins
            is_admin = await util.isAdmin(event)
            if is_admin:
                error_message += (
                    f"**Raw error message (admin only):**\n```\n{original_msg}\n```"
                )
            else:
                # error_message += "You have sent too many requests in a short period. Please wait and try again.\n\n"

                delay_match = re.search(r'"retryDelay":\s*"([^"]+)"', original_msg)
                if delay_match:
                    error_message += (
                        f"**Suggested wait time:** `{delay_match.group(1)}`"
                    )

        try:
            if response_message:
                await response_message.edit(error_message, parse_mode="md")
            else:
                await event.reply(error_message, parse_mode="md")
        except Exception as e:
            print(f"Error while sending/editing rate limit exception message: {e}")
        return True

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
