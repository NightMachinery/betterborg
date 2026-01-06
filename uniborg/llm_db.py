import os
import atexit
import re
import traceback
from sqlalchemy import create_engine, event, Column, Integer, String
from sqlalchemy.orm import sessionmaker
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.declarative import declarative_base
from telethon import events

from uniborg.constants import BOT_META_INFO_PREFIX, GEMINI_API_KEYS
from uniborg.llm_util import (
    send_info_message,
    AutoDeleteMode,
)

# --- Client Instance & In-Memory State ---
# The borg client instance will be populated by `_async_init` in `uniborg/uniborg.py`.
borg = None

# In-memory state for managing the API key setting flow.
# This state is specific to each running process.
AWAITING_KEY_FROM_USERS: dict[int, str] = {}  # {user_id: service_name}
API_KEY_ATTEMPTS = {}


# --- Constants ---
MAX_KEY_ATTEMPTS = 3
API_KEY_CONFIG = {
    "gemini": {
        "name": "Gemini",
        "url": "https://aistudio.google.com/app/apikey",
        "regex": r"^(?P<gemini_key>AIza[0-9A-Za-z_-]{30,50})$",
        "welcome_message": "**Welcome! To use this service, I need a Gemini API key.**",
    },
    "openrouter": {
        "name": "OpenRouter.ai",
        "url": "https://openrouter.ai/keys",
        "regex": r"^(?P<openrouter_key>sk-or-v1-[a-zA-Z0-9]{40,100})$",
        "welcome_message": "**To use OpenRouter models, I need an OpenRouter.ai API key.**",
    },
    "deepseek": {
        "name": "DeepSeek",
        "url": "https://platform.deepseek.com/api_keys",
        "regex": r"^(?P<deepseek_key>sk-[a-f0-9]{30,100})$",
        "welcome_message": "**To use DeepSeek models, I need a DeepSeek API key.**",
    },
    "mistral": {
        "name": "Mistral AI",
        "url": "https://console.mistral.ai/api-keys/",
        "regex": r"^(?P<mistral_key>[a-zA-Z0-9]{30,100})$",
        "welcome_message": "**To use Mistral models, I need a Mistral AI API key.**",
    },
}


# --- Database Setup ---

Base = declarative_base()


class UserApiKey(Base):
    """SQLAlchemy model to store user-specific API keys for various services."""

    __tablename__ = "user_api_keys"
    user_id = Column(Integer, primary_key=True, autoincrement=False)
    service = Column(String, primary_key=True)
    api_key = Column(String, nullable=False)


db_path = os.path.expanduser("~/.borg/llm_api_keys.db")
if not os.path.exists(os.path.dirname(db_path)):
    os.makedirs(os.path.dirname(db_path), exist_ok=True)

engine = create_engine(
    f"sqlite:///{db_path}",
    echo=False,
    connect_args={"timeout": 15},
)


@event.listens_for(engine, "connect")
def set_sqlite_pragma(dbapi_connection, connection_record):
    """
    Enable WAL mode for multi-process concurrency.
    """
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA journal_mode=WAL")
    finally:
        cursor.close()


Base.metadata.create_all(engine)
Session = sessionmaker(bind=engine)


def set_api_key(*, user_id: int, service: str, key: str):
    """
    Saves or updates a user's API key.
    Raises OperationalError if the database is locked.
    """
    session = Session()
    try:
        user_key = (
            session.query(UserApiKey)
            .filter(UserApiKey.user_id == user_id, UserApiKey.service == service)
            .first()
        )
        if user_key:
            user_key.api_key = key
        else:
            user_key = UserApiKey(user_id=user_id, service=service, api_key=key)
            session.add(user_key)
        session.commit()
    except:
        session.rollback()
        raise
    finally:
        session.close()


def get_api_key(
    user_id: int,
    *,
    service: str = "gemini",
) -> str | None:
    """Retrieves a user's API key for a given service."""
    session = Session()
    try:
        result = (
            session.query(UserApiKey)
            .filter(UserApiKey.user_id == user_id, UserApiKey.service == service)
            .first()
        )
        return result.api_key if result else None
    finally:
        session.close()


_GEMINI_ROTATE_KEYS = None
_GEMINI_ROTATE_INDEX = 0
_GEMINI_ROTATE_KEYS_ENABLED_USERS = {"chat": set(), "stt": set()}


def user_gemini_rotate_keys_p(
    user_id: int,
    rotate_keys_p: bool = False,
    *,
    require_enabled_p: bool = True,
    require_global_p: bool = True,
    scope: str = "chat",
) -> bool:
    if str(user_id) != "195391705":
        return False
    if require_global_p and not rotate_keys_p:
        return False
    if require_enabled_p and user_id not in _get_rotate_scope_set(scope):
        return False
    return True


def _get_rotate_scope_set(scope: str) -> set[int]:
    return _GEMINI_ROTATE_KEYS_ENABLED_USERS.setdefault(scope, set())


def gemini_rotate_keys_enabled_p(*, user_id: int, scope: str = "chat") -> bool:
    return user_id in _get_rotate_scope_set(scope)


def toggle_gemini_rotate_keys_enabled(*, user_id: int, scope: str = "chat") -> bool:
    scope_set = _get_rotate_scope_set(scope)
    if user_id in scope_set:
        scope_set.remove(user_id)
        return False
    scope_set.add(user_id)
    return True


def _truncate_key(
    key: str,
    *,
    side_len=10,
) -> str:
    if len(key) <= 2 * side_len + 2:
        return key

    return f"{key[:side_len]}…{key[-side_len:]}"


def _load_gemini_rotate_keys() -> list[tuple[str, int]]:
    path = os.path.expanduser(GEMINI_API_KEYS)
    try:
        with open(path, "r", encoding="utf-8") as f:
            keys = []
            for line_no, line in enumerate(f, start=1):
                key = line.strip()
                if not key or key.startswith("#"):
                    continue
                keys.append((key, line_no))
            return keys
    except FileNotFoundError:
        return []
    except Exception as e:
        print(f"Failed to load Gemini rotate keys: {e}")
        return []


def _get_rotated_gemini_api_key() -> tuple[str, int] | None:
    global _GEMINI_ROTATE_KEYS, _GEMINI_ROTATE_INDEX
    if _GEMINI_ROTATE_KEYS is None:
        _GEMINI_ROTATE_KEYS = _load_gemini_rotate_keys()
    if not _GEMINI_ROTATE_KEYS:
        return None
    key = _GEMINI_ROTATE_KEYS[_GEMINI_ROTATE_INDEX % len(_GEMINI_ROTATE_KEYS)]
    _GEMINI_ROTATE_INDEX = (_GEMINI_ROTATE_INDEX + 1) % len(_GEMINI_ROTATE_KEYS)
    return key


def get_gemini_api_key(
    *,
    user_id: int,
    rotate_keys_p: bool = False,
    service: str = "gemini",
    scope: str = "chat",
) -> str | None:
    if service != "gemini":
        return get_api_key(user_id=user_id, service=service)
    if user_gemini_rotate_keys_p(
        user_id,
        rotate_keys_p,
        require_enabled_p=True,
        require_global_p=True,
        scope=scope,
    ):
        rotated = _get_rotated_gemini_api_key()
        if rotated:
            key, line_no = rotated
            print(
                f"Rotated Gemini API key for user_id={user_id} line={line_no} key={_truncate_key(key)}"
            )
            return key
    return get_api_key(user_id=user_id, service=service)


@atexit.register
def close_db_engine():
    """Disposes of the database engine when the bot stops."""
    engine.dispose()


# --- API Key Flow State Management (In-Memory) ---


def is_awaiting_key(user_id: int) -> bool:
    """Checks if a user is currently in the API key submission flow."""
    return user_id in AWAITING_KEY_FROM_USERS


def get_awaiting_service(user_id: int) -> str | None:
    """Gets the service for which the user is providing a key."""
    return AWAITING_KEY_FROM_USERS.get(user_id)


def cancel_key_flow(user_id: int):
    """Cancels the API key flow for a user."""
    AWAITING_KEY_FROM_USERS.pop(user_id, None)
    API_KEY_ATTEMPTS.pop(user_id, None)


# --- Reusable Handler Logic ---


async def request_api_key_message(event, service: str = "gemini"):
    """Sends the instructional message to a user to ask for their API key."""
    if not borg:
        print("Error: llm_db.borg not set. This should never happen.")
        return

    config = API_KEY_CONFIG.get(service)
    if not config:
        print(f"Invalid service '{service}' requested for API key.")
        return

    user_id = event.sender_id
    AWAITING_KEY_FROM_USERS[user_id] = service
    API_KEY_ATTEMPTS[user_id] = 0

    key_request_message = (
        f"{config['welcome_message']}\n\n"
        f"You can get a free API key from here:\n"
        f"➡️ **{config['url']}** ⬅️\n\n"
        "Once you have your key, please send it to me in the next message.\n\n"
        "(Type `cancel` to stop this process.)"
    )
    try:
        # Send PM directly to the user
        await borg.send_message(
            user_id, f"{BOT_META_INFO_PREFIX}{key_request_message}", link_preview=False
        )

        if hasattr(event, "reply") and not event.is_private:
            await send_info_message(event, "I've sent you a private message for setup.")
    except Exception as e:
        print(f"Could not send PM to {user_id}. Error: {e}")

        cancel_key_flow(user_id)

        if hasattr(event, "reply"):
            await send_info_message(
                event,
                "I couldn't send you a private message. "
                "Send `/start` to me in a private chat and setup a (free) API key to use me.",
                auto_delete=AutoDeleteMode.GROUP_ONLY,
            )

    raise events.StopPropagation
    # If this exception is raised in any of the handlers for a given event, it will stop the execution of all other registered event handlers. It can be seen as the StopIteration in a for loop but for events.


async def _save_key_with_error_handling(event, user_id, service, key):
    """Wraps set_api_key with user-facing error handling."""
    try:
        set_api_key(user_id=user_id, service=service, key=key)
        return True
    except OperationalError as e:
        if "database is locked" in str(e).lower():
            await send_info_message(
                event, "The database is currently busy. Please try again in a moment."
            )
        else:
            await send_info_message(
                event, "A database error occurred. Please report this to the developer."
            )
            print(f"Database error for user {user_id}: {traceback.format_exc()}")
        return False
    except Exception:
        await send_info_message(
            event, "An unexpected error occurred while saving your key."
        )
        print(
            f"Unexpected error saving key for user {user_id}: {traceback.format_exc()}"
        )
        return False


async def handle_set_key_command(event, service: str):
    """High-level logic for the /set<service>key command."""
    config = API_KEY_CONFIG.get(service)
    if not config:
        return

    api_key_match = event.pattern_match.group(1)
    user_id = event.sender_id

    if api_key_match and api_key_match.strip():
        api_key = api_key_match.strip()
        if not re.match(config["regex"], api_key):
            await send_info_message(
                event,
                f"The provided API key has an invalid format for {config['name']}. Please check and try again.",
            )
            return

        if await _save_key_with_error_handling(event, user_id, service, api_key):
            cancel_key_flow(user_id)
            await event.delete()
            confirmation_message = f"✅ Your {config['name']} API key has been saved. Your message was deleted for security."
            try:
                # Send PM confirmation
                await borg.send_message(
                    user_id, f"{BOT_META_INFO_PREFIX}{confirmation_message}"
                )

                if not event.is_private:
                    await send_info_message(
                        event, "I've confirmed your key update in a private message."
                    )
            except Exception:
                await send_info_message(event, confirmation_message, reply_to=False)
    else:
        await request_api_key_message(event, service)


async def handle_key_submission(
    event,
    *,
    success_msg="You can now use this bot.",
):
    """High-level logic for handling a plain-text API key submission."""
    user_id = event.sender_id
    text = event.text.strip()
    service = get_awaiting_service(user_id)

    if not service:
        return

    config = API_KEY_CONFIG[service]

    if text.lower() == "cancel":
        cancel_key_flow(user_id)
        await send_info_message(
            event,
            f"API key setup has been cancelled. You can start again with /set{service}key.",
        )
        return

    if not re.match(config["regex"], text):
        API_KEY_ATTEMPTS[user_id] = API_KEY_ATTEMPTS.get(user_id, 0) + 1
        if API_KEY_ATTEMPTS[user_id] >= MAX_KEY_ATTEMPTS:
            cancel_key_flow(user_id)
            await send_info_message(
                event,
                f"Too many invalid attempts. The API key setup has been cancelled. You can try again later with /set{service}key.",
            )
        else:
            remaining = MAX_KEY_ATTEMPTS - API_KEY_ATTEMPTS[user_id]
            await send_info_message(
                event,
                f"This does not look like a valid {config['name']} API key. Please try again. You have {remaining} attempt(s) left.",
            )
        return

    if await _save_key_with_error_handling(event, user_id, service, text):
        cancel_key_flow(user_id)
        await event.delete()
        await send_info_message(
            event,
            f"✅ Your {config['name']} API key has been saved. Your message was deleted for security.\n"
            + success_msg,
            reply_to=False,
        )
