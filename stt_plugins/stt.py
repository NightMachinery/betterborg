from uniborg import util
import os
import traceback
import atexit
import llm
import time
import uuid
from datetime import datetime
from telethon import events
from sqlalchemy import create_engine, Column, Integer, String
from sqlalchemy.orm import sessionmaker
from sqlalchemy.ext.declarative import declarative_base

# Assuming 'borg' is the initialized Telethon client and 'util' is a module
# with helper functions, as is common in user-bot frameworks.
# from uniborg import util, borg

# --- SQLAlchemy Database Setup for API Keys ---

Base = declarative_base()


class UserApiKey(Base):
    """SQLAlchemy model to store user-specific API keys for various services."""

    __tablename__ = "user_api_keys"
    user_id = Column(Integer, primary_key=True, autoincrement=False)
    service = Column(String, primary_key=True)
    api_key = Column(String, nullable=False)


# Create an SQLite database engine and session
db_path = os.path.expanduser("~/.borg/llm_api_keys.db")
if not os.path.exists(os.path.dirname(db_path)):
    os.makedirs(os.path.dirname(db_path), exist_ok=True)

engine = create_engine(f"sqlite:///{db_path}", echo=False)
Base.metadata.create_all(engine)
Session = sessionmaker(bind=engine)
session = Session()


def set_api_key(*, user_id, service, key):
    """Saves or updates a user's API key for a given service."""
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


def get_api_key(*, user_id, service):
    """Retrieves a user's API key for a given service."""
    result = (
        session.query(UserApiKey)
        .filter(UserApiKey.user_id == user_id, UserApiKey.service == service)
        .first()
    )
    return result.api_key if result else None


@atexit.register
def close_db_session():
    """Ensures the database session is closed when the bot stops."""
    session.close()


# --- Core Transcription Logic ---

TRANSCRIPTION_PROMPT = """Transcribe this audio word-for-word, following these rules:

1. Language will be either:
  - Farsi/Persian, or
  - English with an Iranian accent

2. Include only meaningful speech content:
  - Skip filler words and hesitation markers (um, uh, er, like, you know, "P", etc.)
  - Skip false starts, repetitions, and cut-off words
  - Skip all music and sound effects
  - Skip discourse markers (well, I mean, you know)
  - Omit words when in doubt

3. Format:
  - Pure transcription only
    - Add appropriate punctuation marks to make the transcription easier to read
  - No comments
  - No timestamps
  - No explanatory notes"""


async def llm_stt(*, cwd, event, model_name="gemini-2.5-flash", log=True):
    """
    Performs speech-to-text on media files using the llm library and Gemini,
    replying with the result or an error message.
    """
    api_key = get_api_key(user_id=event.sender_id, service="gemini")
    if not api_key:
        await event.reply("Your Gemini API key is not set. Please use `/setGeminiKey YOUR_KEY`.")
        return

    try:
        # Assuming the llm-gemini plugin is installed
        model = llm.get_async_model(model_name)
    except llm.UnknownModelError:
        await event.reply(
            f"Error: '{model_name}' model not found. Perhaps the relevant LLM plugin has not been installed."
        )
        return
    except Exception as e:
        print(traceback.format_exc())
        print(e)

        await event.reply(f"An unexpected error occurred while loading the model.")
        return

    attachments = []
    try:
        for filename in os.listdir(cwd):
            filepath = os.path.join(cwd, filename)
            if not os.path.isfile(filepath):
                continue

            # Handle mime-type for ogg/oga specifically
            if filename.lower().endswith((".ogg", ".oga")):
                with open(filepath, "rb") as f:
                    attachments.append(llm.Attachment(content=f.read(), type="audio/ogg"))
            else:
                attachments.append(llm.Attachment(path=filepath))
    except Exception as e:
        print(e)
        print(traceback.format_exc())

        await event.reply(f"Error while preparing media files for transcription")
        return

    if not attachments:
        # This case is unlikely if the media handler triggers only on media, but it's good practice
        await event.reply("No valid media files found to transcribe.")
        return

    status_message = await event.reply("Transcribing...")
    try:
        response = await model.prompt(
            prompt=TRANSCRIPTION_PROMPT,
            attachments=attachments,
            key=api_key,
            temperature=0,
        )
        transcription = await response.text()
        await status_message.edit(transcription)

        # --- Logging Logic ---
        if log:
            try:
                # Generate a unique filename with timestamp and UUID
                timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
                unique_id = str(uuid.uuid4())
                log_filename = f"{timestamp}_{unique_id}.txt"

                user = event.sender
                user_id = user.id
                first_name = user.first_name or ""
                last_name = user.last_name or ""
                username = user.username or "N/A"
                full_name = f"{first_name} {last_name}".strip()

                log_content = (
                    f"Date: {timestamp}\n"
                    f"User ID: {user_id}\n"
                    f"Name: {full_name}\n"
                    f"Username: @{username}\n"
                    f"model: {model_name}\n"
                )
                print(f"\n{log_content}\n---")

                log_content += (
                    f"--- Transcription ---\n"
                    f"{transcription}"
                )

                log_dir = os.path.expanduser(f"~/.borg/stt/log/{user_id}")
                os.makedirs(log_dir, exist_ok=True)

                log_file_path = os.path.join(log_dir, log_filename)

                with open(log_file_path, "w", encoding="utf-8") as f:
                    f.write(log_content)

            except Exception as log_e:
                # Log the logging error to stderr to not disturb the user flow
                print(f"Failed to write transcription log: {log_e}")
                print(traceback.format_exc())

    except Exception as e:
        print(e)
        print(traceback.format_exc())

        await status_message.edit(f"An error occurred during the API call.")


# --- Telethon Event Handlers ---

PROCESSED_GROUP_IDS = set()

@borg.on(events.NewMessage(pattern=r"/setGeminiKey\s+(.+)"))
async def set_key_handler(event):
    """Handles the /setGeminiKey command to save the user's API key."""
    api_key = event.pattern_match.group(1).strip()
    set_api_key(user_id=event.sender_id, service="gemini", key=api_key)
    await event.delete()
    await borg.send_message(
        event.chat_id,
        "Your Gemini API key has been saved. Your message was deleted for security.",
    )


@borg.on(events.NewMessage())
async def media_handler(event):
    """
    Handles incoming messages with media, ensuring grouped media is processed only once.
    """
    if not event.sender or not event.media:
        return

    group_id = event.grouped_id
    # If the message is part of a group...
    if group_id:
        # ...and we are already processing this group, stop.
        if group_id in PROCESSED_GROUP_IDS:
            return

        # ...otherwise, "lock" this group and process it.
        PROCESSED_GROUP_IDS.add(group_id)
        try:
            # Rely on the framework to handle downloading all media in the group.
            await util.run_and_upload(event=event, to_await=llm_stt)
        finally:
            # Wait for a few seconds before unlocking the group to prevent race conditions.
            await asyncio.sleep(5)

            # Once done (or if an error occurs), "unlock" the group.
            PROCESSED_GROUP_IDS.remove(group_id)
    else:
        # If it's not a grouped message, process it directly.
        await util.run_and_upload(event=event, to_await=llm_stt)
