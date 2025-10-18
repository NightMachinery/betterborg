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
from google import genai
from google.genai import types
import asyncio
from enum import Enum


# --- Info Message Helpers ---


class AutoDeleteMode(str, Enum):
    """Mode for auto-deleting info messages."""

    DISABLED = "disabled"
    GROUP_ONLY = "group_only"
    ALWAYS = "always"


# Auto-delete time for info messages (in seconds)
AUTO_DELETE_TIME = 30


async def auto_delete_info_message(
    event,
    message,
    delay: int = AUTO_DELETE_TIME,
    *,
    auto_delete_override_p: "AutoDeleteMode | bool | str" = "MAGIC_FROM_CALLABLE",
    get_auto_delete_mode=None,
):
    """Auto-deletes info messages based on chat settings or override.

    Args:
        event: The Telegram event
        message: The message object to potentially delete
        delay: Seconds to wait before deleting
        auto_delete_override_p: Override for auto-deletion behavior:
            - "MAGIC_FROM_CALLABLE" (default): Use get_auto_delete_mode callable if provided, else DISABLED
            - AutoDeleteMode enum value: Explicit mode (DISABLED/GROUP_ONLY/ALWAYS)
            - bool: True = ALWAYS, False = DISABLED
        get_auto_delete_mode: Optional callable that takes chat_id and returns AutoDeleteMode
    """
    if auto_delete_override_p == "MAGIC_FROM_CALLABLE":
        if get_auto_delete_mode is not None:
            auto_delete_mode = get_auto_delete_mode(event.chat_id)
        else:
            auto_delete_mode = AutoDeleteMode.DISABLED
    elif isinstance(auto_delete_override_p, AutoDeleteMode):
        auto_delete_mode = auto_delete_override_p
    else:
        # Convert bool to enum
        auto_delete_mode = (
            AutoDeleteMode.ALWAYS if auto_delete_override_p else AutoDeleteMode.DISABLED
        )

    should_delete = auto_delete_mode == AutoDeleteMode.ALWAYS or (
        auto_delete_mode == AutoDeleteMode.GROUP_ONLY and not event.is_private
    )

    if should_delete:
        await asyncio.sleep(delay)
        try:
            await message.delete()
        except Exception:
            pass  # Silently ignore deletion errors


async def send_info_message(
    event,
    text: str,
    *,
    auto_delete: "AutoDeleteMode | bool | str" = False,
    delay: int = AUTO_DELETE_TIME,
    prefix: str = BOT_META_INFO_PREFIX,
    reply_to=True,
    get_auto_delete_mode=None,
    **kwargs,
):
    """Sends an info message with automatic prefix and optional auto-deletion.

    Args:
        event: The event to reply to
        text: Message text (prefix will be prepended automatically)
        auto_delete: Auto-delete control - False (default, no delete), True (always delete),
                    "MAGIC_FROM_CALLABLE" (use get_auto_delete_mode), or AutoDeleteMode enum value
        delay: Delay before deletion in seconds
        prefix: Prefix to prepend (default: BOT_META_INFO_PREFIX)
        reply_to: If True, uses event.reply(); otherwise uses event.respond(reply_to=...)
                 with the provided value (False/None/int/Message)
        get_auto_delete_mode: Optional callable that takes chat_id and returns AutoDeleteMode
        **kwargs: Additional arguments passed to reply/respond (parse_mode, link_preview, etc.)

    Returns:
        The sent message object
    """
    full_text = f"{prefix}{text}"

    if reply_to is True:
        msg = await event.reply(full_text, **kwargs)
    else:
        # reply_to can be False, None, int (message ID), or Message object
        msg = await event.respond(full_text, reply_to=reply_to, **kwargs)

    await auto_delete_info_message(
        event,
        msg,
        delay,
        auto_delete_override_p=auto_delete,
        get_auto_delete_mode=get_auto_delete_mode,
    )

    return msg


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

    if GEMINI_SPECIAL_HTTP_PROXY_ADMIN_ONLY_P and not util.is_admin_by_id(
        user_id,
        additional_admins=[
            467602588,
        ],
    ):
        raise ProxyRestrictedException(
            "🚫 This Gemini feature is currently unavailable due to regional restrictions. "
            "Our servers operate within EU jurisdiction where certain advanced AI capabilities "
            "require special compliance measures. Please try again later or contact support."
        )

    return proxy_url, None


def create_genai_client(
    api_key: str,
    *,
    user_id: int = None,
    read_bufsize: int = None,
    proxy_p: bool = False,
) -> "genai.Client":
    """
    Creates and configures a genai.Client with optional proxy support and buffer size.

    Args:
        api_key: The Google Gemini API key.
        user_id: Optional. The ID of the user making the request, for proxy access checks.
        read_bufsize: Optional. The read buffer size in bytes for the async client.
        proxy_p: Whether to use proxy configuration. Defaults to False.

    Returns:
        A configured google.genai.Client instance.
    """
    client_args = {}
    async_client_args = {}

    if proxy_p and user_id is not None:
        proxy_url, _ = get_proxy_config_or_error(user_id)
        if proxy_url:
            client_args["proxy"] = proxy_url
            async_client_args["proxy"] = proxy_url
            print(f"LLM_Util: Using proxy {proxy_url} for user {user_id}")

    if read_bufsize is not None:
        #: @G25 The buffer size is per concurrent request. Setting it to 100MB means that if you have, for example, 5 simultaneous live sessions running, you could see up to 500MB of memory being used just for these buffers during active data streaming. This is why it's a trade-off between preventing the "Chunk too big" error and managing server memory efficiently.
        async_client_args["read_bufsize"] = read_bufsize

    http_options = types.HttpOptions(
        client_args=client_args or None,
        async_client_args=async_client_args or None,
    )

    return genai.Client(api_key=api_key, http_options=http_options)


# --- Prompt Loading Utilities ---

# Environment and prompt loading setup
NIGHTDIR = os.getenv("NIGHTDIR", None)
DEFAULT_PROMPT_DIR = f"{NIGHTDIR}/PE" if NIGHTDIR else "UNSET"


def load_prompt_from_file(filename, *, prefix=DEFAULT_PROMPT_DIR):
    """Load prompt content from file. Returns empty string and prints warning if prefix is UNSET."""
    if prefix == "UNSET":
        print(f"Warning: NIGHTDIR not set, cannot load prompt from {filename}")
        return ""

    try:
        file_path = Path(prefix) / filename
        return file_path.read_text()
    except FileNotFoundError:
        print(f"Warning: Prompt file not found: {file_path}")
        return ""
    except Exception as e:
        print(f"Warning: Error loading prompt from {file_path}: {e}")
        return ""


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
        try:
            return json.loads(valid_json)
        except:
            return None

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

                                if quota_metric:
                                    error_message += f"**Quota Metric:** {quota_metric.replace('_', ' ').title()}\n"

                                if quota_id:
                                    error_message += f"**Quota ID:** {quota_id}\n"

                                if quota_value:
                                    error_message += f"**Quota Value:** {quota_value}\n"

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

            finally:
                # Show full JSON for admins
                is_admin = await util.isAdmin(event)
                if is_admin:
                    formatted_json = json.dumps(error_data, indent=2)
                    error_message += f"**Full error details (admin only):**\n```\n{formatted_json}\n```"

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
                await util.edit_message(
                    response_message, error_message, parse_mode="md", append_p=True
                )
            else:
                await util.discreet_send(
                    event, error_message, reply_to=event.message, parse_mode="md"
                )
        except Exception as e:
            traceback.print_exc()
            print(f"Error while sending/editing rate limit exception message: {e}")
        return True

    error_message = f"{BOT_META_INFO_PREFIX}\n\n{str(exception)}\n"
    #: Not putting the code block markers allows more flexibility in the error message formatting. The caller can format its exception itself.
    #: Note that we are only printing the exception if it is an explicit TelegramUserReplyException.
    # error_message = f"{BOT_META_INFO_PREFIX}\n```\n{str(exception)}\n```"

    if isinstance(exception, TelegramUserReplyException):
        try:
            if response_message:
                await util.edit_message(
                    response_message,
                    error_message,
                    parse_mode="md",
                    append_p=True,
                )
            else:
                await util.discreet_send(
                    event,
                    error_message,
                    reply_to=event.message,
                    parse_mode="md",
                )
        except Exception as e:
            traceback.print_exc()
            print(f"Error while sending/editing user reply exception message: {e}")
        return True

    # Special handling for invalid/suspended API keys
    error_message_lower = error_message.lower()

    # Check for various API key error patterns
    api_key_error_patterns = [
        "api key not valid",
        "consumer_suspended",
        "consumer has been suspended",
        "permission denied: consumer",
        "api_key_invalid",
        "invalid api key",
    ]

    is_api_key_error = any(
        pattern in error_message_lower for pattern in api_key_error_patterns
    )

    if service and is_api_key_error:
        # Determine if we should show detailed error info
        is_admin = await util.isAdmin(event)
        is_private = event.is_private
        should_show_details = is_admin and is_private

        # Get the service-specific setkey command
        service_lower = service.lower()
        if service_lower == "gemini":
            setkey_command = "/setGeminiKey"
        elif service_lower == "openrouter":
            setkey_command = "/setOpenRouterKey"
        else:
            setkey_command = f"/set{service.title()}Key"

        # Extract error information from JSON if available
        original_msg = str(exception)
        error_data = h_extract_exception_json_safely(original_msg)
        error_detail_msg = None
        error_reason = None

        if error_data:
            try:
                error_info = error_data.get("error", {})
                error_status = error_info.get("status", "")
                error_detail_msg = error_info.get("message", "")

                # Check for CONSUMER_SUSPENDED specifically
                if error_status == "PERMISSION_DENIED":
                    details = error_info.get("details", [])
                    for detail in details:
                        if detail.get("reason") == "CONSUMER_SUSPENDED":
                            error_reason = "CONSUMER_SUSPENDED"
                            break
            except Exception as parse_err:
                print(f"Error parsing API key error details: {parse_err}")

        # Build user-friendly message
        if error_reason == "CONSUMER_SUSPENDED":
            user_friendly_message = (
                f"{BOT_META_INFO_PREFIX}❌ **API Key Error: Account Suspended**\n\n"
                f"Your {service.title()} API key's associated account has been suspended.\n\n"
                f"**Possible reasons:**\n"
                f"• Billing issue or payment method problem\n"
                f"• Terms of service violation\n"
                f"• Account verification required\n"
                f"• Key expired or revoked\n\n"
                f"**Next steps:**\n"
                f"1. Check your {service.title()} account status\n"
                f"2. Verify billing and payment information\n"
                f"3. Create a new API key if the old one is invalid\n"
                f"4. Update the bot with your new API key using `{setkey_command}`\n"
            )
        else:
            # Generic API key error
            user_friendly_message = (
                f"{BOT_META_INFO_PREFIX}❌ **API Key Error: Invalid or Permission Denied**\n\n"
                f"Your {service.title()} API key is invalid or doesn't have the necessary permissions.\n\n"
                f"**Next steps:**\n"
                f"1. Verify your {service.title()} API key is correct\n"
                f"2. Check your API key permissions and billing status\n"
                f"3. Create a new API key if needed\n"
                f"4. Update the bot using `{setkey_command}`\n"
            )

        # Add error details if admin and private
        if should_show_details and error_detail_msg:
            user_friendly_message += (
                f"\n**Error details (admin only):** {error_detail_msg}"
            )

        # Send the message
        try:
            if response_message:
                await util.edit_message(
                    response_message,
                    user_friendly_message,
                    parse_mode="md",
                    append_p=True,
                )
            else:
                await util.discreet_send(
                    event,
                    user_friendly_message,
                    reply_to=event.message,
                    parse_mode="md",
                )
        except Exception as e:
            print(f"Error sending API key error message: {e}")

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
            await util.edit_message(
                response_message,
                user_facing_error,
                append_p=True,
                parse_mode="md",
            )
        else:
            await util.discreet_send(
                event, user_facing_error, reply_to=event.message, parse_mode="md"
            )
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
            await util.discreet_send(
                event, configured_message, reply_to=event.message, parse_mode="md"
            )
        else:
            # If not configured, start the key request flow
            await llm_db.request_api_key_message(event, service)

    return start_handler


def get_service_from_model(model: str) -> str:
    """
    Determines the service needed based on the model name.

    Args:
        model: The model name (e.g., "openrouter/openai/gpt-5-chat", "deepseek/deepseek-chat")

    Returns:
        str: The service name ("openrouter", "openai", "deepseek", "mistral", or "gemini" as default)
    """
    if model.startswith("openrouter/"):
        return "openrouter"
    elif model.startswith("openai/"):
        return "openai"
    elif model.startswith("deepseek/"):
        return "deepseek"
    elif model.startswith("mistral/"):
        return "mistral"
    else:
        return "gemini"


def truncate_text_for_llm(
    text: str,
    *,
    mode: str = "start_end",
    to_length: int = 10000,
    semantic_boundaries_p: bool = False,
    start_split=0.6,
) -> str:
    """
    Intelligently truncate text for LLM processing, preserving meaningful context.

    Args:
        text: The text to truncate
        mode: Truncation mode - "start_end" includes both beginning and end
        to_length: Target length for truncated text

    Returns:
        Truncated text with indication if content was truncated
    """
    if len(text) <= to_length:
        return text

    if mode == "start_end":
        # Reserve space for truncation indicator
        truncation_msg = "\n\n[... content truncated for brevity ...]\n\n"
        available_length = to_length - len(truncation_msg)

        # Split available space between start and end (60/40 split)
        start_length = int(available_length * start_split)
        end_length = available_length - start_length

        # Find good split points (prefer sentence/paragraph boundaries)
        start_text = text[:start_length]
        end_text = text[-end_length:]

        if semantic_boundaries_p:
            # Try to split at sentence boundaries for cleaner truncation
            for boundary in ["\n\n", "\n", ". ", "! ", "? "]:
                # Adjust start boundary
                boundary_pos = start_text.rfind(boundary)
                if boundary_pos > start_length * 0.7:  # Don't truncate too aggressively
                    start_text = start_text[: boundary_pos + len(boundary)]
                    break

            # Adjust end boundary
            for boundary in ["\n\n", "\n", ". ", "! ", "? "]:
                boundary_pos = end_text.find(boundary)
                if boundary_pos != -1 and boundary_pos < end_length * 0.3:
                    end_text = end_text[boundary_pos:]
                    break

        return start_text.strip() + truncation_msg + end_text.strip()

    else:
        # Default fallback: simple truncation
        return text[:to_length] + "..."
