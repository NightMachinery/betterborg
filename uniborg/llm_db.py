import os
import atexit
import re
import traceback
from sqlalchemy import create_engine, event, Column, Integer, String
from sqlalchemy.orm import sessionmaker
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.declarative import declarative_base

from uniborg.llm_util import BOT_META_INFO_PREFIX

# --- Client Instance & In-Memory State ---
# The borg client instance will be populated by `_async_init` in `uniborg/uniborg.py`.
borg = None

# In-memory state for managing the API key setting flow.
# This state is specific to each running process.
AWAITING_KEY_FROM_USERS = set()
API_KEY_ATTEMPTS = {}


# --- Constants ---
GEMINI_API_KEY_REGEX = r"^(?P<gemini_key>AIza[0-9A-Za-z_-]{30,50})$"
MAX_KEY_ATTEMPTS = 3


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


def get_api_key(*, user_id: int, service: str) -> str | None:
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


@atexit.register
def close_db_engine():
    """Disposes of the database engine when the bot stops."""
    engine.dispose()


# --- API Key Flow State Management (In-Memory) ---


def is_awaiting_key(user_id: int) -> bool:
    """Checks if a user is currently in the API key submission flow."""
    return user_id in AWAITING_KEY_FROM_USERS


def cancel_key_flow(user_id: int):
    """Cancels the API key flow for a user."""
    AWAITING_KEY_FROM_USERS.discard(user_id)
    API_KEY_ATTEMPTS.pop(user_id, None)


# --- Reusable Handler Logic ---


async def request_api_key_message(event):
    """Sends the instructional message to a user to ask for their API key."""
    if not borg:
        print("Error: llm_db.borg not set. This should never happen.")
        return

    user_id = event.sender_id
    AWAITING_KEY_FROM_USERS.add(user_id)
    API_KEY_ATTEMPTS[user_id] = 0

    key_request_message = (
        f"{BOT_META_INFO_PREFIX}**Welcome! To use this service, I need a Gemini API key.**\n\n"
        "You can get a free API key from Google AI Studio:\n"
        "➡️ **https://aistudio.google.com/app/apikey** ⬅️\n\n"
        "Once you have your key, please send it to me in the next message.\n\n"
        "(Type `cancel` to stop this process.)"
    )
    try:
        await borg.send_message(user_id, key_request_message, link_preview=False)
        if hasattr(event, "reply") and not event.is_private:
            await event.reply(
                f"{BOT_META_INFO_PREFIX}I've sent you a private message for setup."
            )
    except Exception as e:
        print(f"Could not send PM to {user_id}. Error: {e}")
        if hasattr(event, "reply"):
            await event.reply(
                f"{BOT_META_INFO_PREFIX}I couldn't send you a private message. Please check your privacy settings, "
                "then send `/start` to me in a private chat."
            )


async def _save_key_with_error_handling(event, user_id, service, key):
    """Wraps set_api_key with user-facing error handling."""
    try:
        set_api_key(user_id=user_id, service=service, key=key)
        return True
    except OperationalError as e:
        if "database is locked" in str(e).lower():
            await event.reply(
                f"{BOT_META_INFO_PREFIX}The database is currently busy. Please try again in a moment."
            )
        else:
            await event.reply(
                f"{BOT_META_INFO_PREFIX}A database error occurred. Please report this to the developer."
            )
            print(f"Database error for user {user_id}: {traceback.format_exc()}")
        return False
    except Exception:
        await event.reply(
            f"{BOT_META_INFO_PREFIX}An unexpected error occurred while saving your key."
        )
        print(
            f"Unexpected error saving key for user {user_id}: {traceback.format_exc()}"
        )
        return False


async def handle_set_key_command(event):
    """High-level logic for the /setgeminikey command."""
    api_key_match = event.pattern_match.group(1)
    user_id = event.sender_id

    if api_key_match and api_key_match.strip():
        api_key = api_key_match.strip()
        if not re.match(GEMINI_API_KEY_REGEX, api_key):
            await event.reply(
                f"{BOT_META_INFO_PREFIX}The provided API key has an invalid format. Please check and try again."
            )
            return

        if await _save_key_with_error_handling(event, user_id, "gemini", api_key):
            cancel_key_flow(user_id)
            await event.delete()
            confirmation_message = f"{BOT_META_INFO_PREFIX}✅ Your Gemini API key has been saved. Your message was deleted for security."
            try:
                await borg.send_message(user_id, confirmation_message)
                if not event.is_private:
                    await event.reply(
                        f"{BOT_META_INFO_PREFIX}I've confirmed your key update in a private message."
                    )
            except Exception:
                await event.respond(confirmation_message)
    else:
        await request_api_key_message(event)


async def handle_key_submission(
    event,
    *,
    success_msg="You can now use this bot.",
):
    """High-level logic for handling a plain-text API key submission."""
    user_id = event.sender_id
    text = event.text.strip()

    if text.lower() == "cancel":
        cancel_key_flow(user_id)
        await event.reply(
            f"{BOT_META_INFO_PREFIX}API key setup has been cancelled. You can start again with /setgeminikey."
        )
        return

    if not re.match(GEMINI_API_KEY_REGEX, text):
        API_KEY_ATTEMPTS[user_id] = API_KEY_ATTEMPTS.get(user_id, 0) + 1
        if API_KEY_ATTEMPTS[user_id] >= MAX_KEY_ATTEMPTS:
            cancel_key_flow(user_id)
            await event.reply(
                f"{BOT_META_INFO_PREFIX}Too many invalid attempts. The API key setup has been cancelled. You can try again later with /setgeminikey."
            )
        else:
            remaining = MAX_KEY_ATTEMPTS - API_KEY_ATTEMPTS[user_id]
            await event.reply(
                f"{BOT_META_INFO_PREFIX}This does not look like a valid API key. Please try again. You have {remaining} attempt(s) left."
            )
        return

    if await _save_key_with_error_handling(event, user_id, "gemini", text):
        cancel_key_flow(user_id)
        await event.delete()
        await event.respond(
            f"{BOT_META_INFO_PREFIX}✅ Your Gemini API key has been saved. Your message was deleted for security.\n"
            + success_msg
        )
