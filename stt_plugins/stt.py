from icecream import ic
from uniborg import util
import os
import traceback
import atexit
import llm
import time
import uuid
import asyncio
import re
import json
from datetime import datetime
from telethon import events
from telethon.tl.functions.bots import SetBotCommandsRequest
from telethon.tl.types import BotCommand, BotCommandScopeDefault
from sqlalchemy import create_engine, Column, Integer, String
from sqlalchemy.orm import sessionmaker
from sqlalchemy.ext.declarative import declarative_base
from pydantic import BaseModel, Field
from typing import Optional

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

# --- State Management for API Key Flow ---
AWAITING_KEY_FROM_USERS = set()
API_KEY_ATTEMPTS = {}
GEMINI_API_KEY_REGEX = r"^(?P<gemini_key>AIza[0-9A-Za-z_-]{30,50})$"

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

# --- Pydantic Schema for a SINGLE, Combined Transcription ---

class TranscriptionResult(BaseModel):
    """The synthesized processing result for ALL provided media files."""
    transcription: str = Field(
        description="The combined verbatim audio transcription or OCR text from all files. Separate content from different files with '---'. Empty if no speech/text is found."
    )
    visual_description: Optional[str] = Field(
        None,
        description="For video(s) ONLY, a combined narrative of the key visual scenes from all videos. MUST be null otherwise."
    )
    output_type: str = Field(
        "none",
        description="The dominant output type. One of: 'transcript' (if any audio/video), 'ocr' (if only images), or 'none'."
    )
    error_message: Optional[str] = Field(
        None,
        description="If processing failed entirely, provide a brief error message here."
    )

# --- New System Prompt for a Single, Synthesized JSON Output ---

TRANSCRIPTION_PROMPT_V6 = r"""
Your mission is to act as a media processing engine. Analyze ALL attached media files and synthesize their content into a SINGLE structured JSON object.

Your entire output MUST be a single, valid JSON object that conforms to the `TranscriptionResult` schema provided.

Follow these synthesis rules:
1.  **`transcription` field**:
    - For all files, combine their meaningful content into this single field.
    - **Content Rules:**
        - **Language:** The language will likely be Farsi/Persian or English with an Iranian accent. Prioritize transcribing these accurately. If you are SURE the language is something else, transcribe it in its original language and translate it to English. (Farsi does not need translation.)
        - **Inclusion:** Transcribe spoken words from audio/video, lyrics from songs (if they are the primary content, not background music), and text from images (OCR).
        - **Exclusion:** Skip filler words (um, uh, er), false starts, repetitions, non-speech sounds (music/effects if speech is present), and discourse markers (well, I mean). Omit words when in doubt.
        - **Formatting:**
            - **Readability:** Use standard punctuation (commas, periods) and create new paragraphs for different topics or speakers to make the text easy to read. Maintain the spatial structure of the text when doing OCR using appropriate whitespace etc.
            - **Separators:** If you process multiple files, you MUST place `---` on its own line to separate the content from each distinct file.
            - **Prohibited Content:** You MUST NOT include timestamps, explanatory notes (e.g., "[music playing]"), or any commentary in the transcription text.

2.  **`visual_description` field**:
    - If any of the files are videos, provide a combined, flowing description of their key visual elements in this field. Ignore the visuals for all other file types.
    - If there are NO videos, this field MUST be `null`.

3.  **`output_type` field**:
    - Set to "transcript" if any audio or video files are present.
    - Set to "ocr" if ONLY image files are present.
    - Set to "none" if no text/speech can be extracted from any file.

4.  **Failures**:
    - If all files are unintelligible or empty, set `transcription` to an empty string, `output_type` to "none", and optionally provide a reason in `error_message`.

Do not add any commentary, apologies, or text outside of the final JSON object.
"""

# Set this as the active prompt
TRANSCRIPTION_PROMPT = TRANSCRIPTION_PROMPT_V6
print(f"Prompt:\n\n{TRANSCRIPTION_PROMPT}\n---\n\n")


# --- Core Transcription Logic ---

async def request_api_key(event):
    """
    Initiates the flow to ask a user for their API key.
    Sends a message with instructions and adds the user's ID to the awaiting set.
    """
    user_id = event.sender_id
    AWAITING_KEY_FROM_USERS.add(user_id)
    API_KEY_ATTEMPTS[user_id] = 0

    key_request_message = (
        "**Welcome! To use the transcription service, I need a Gemini API key.**\n\n"
        "You can get a free API key from Google AI Studio:\n"
        "➡️ **https://aistudio.google.com/app/apikey** ⬅️\n\n"
        "Once you have your key, please send it to me in the next message.\n\n"
        "(Type `cancel` to stop this process.)"
    )

    try:
        await borg.send_message(user_id, key_request_message, link_preview=False)
        if not event.is_private:
            await event.reply("I've sent you a private message for setup.")
    except Exception as e:
        print(f"Could not send PM to {user_id}. Error: {e}")
        await event.reply(
            "I couldn't send you a private message. Please check your privacy settings, "
            "then send `/start` to me."
        )

def cancel_key_flow(user_id):
    """Removes a user from the API key waiting flow and resets their attempts."""
    AWAITING_KEY_FROM_USERS.discard(user_id)
    API_KEY_ATTEMPTS.pop(user_id, None)


async def llm_stt(*, cwd, event, model_name="gemini-2.5-flash", log=True):
    """
    Performs speech-to-text on media, enforcing a single structured JSON output
    that synthesizes all provided files.
    """
    api_key = get_api_key(user_id=event.sender_id, service="gemini")
    if not api_key:
        await request_api_key(event)
        return

    try:
        model = llm.get_async_model(model_name)
        if not getattr(model, 'supports_schema', False):
             await event.reply(f"Error: The model '{model_name}' does not support structured output (schemas).")
             return
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

            # Handle specific audio formats that might need explicit typing
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
        await event.reply("No valid media files found to transcribe.")
        return

    status_message = await event.reply("Transcribing...")

    try:
        # Pass the single TranscriptionResult schema directly
        response = await model.prompt(
            prompt=TRANSCRIPTION_PROMPT,
            attachments=attachments,
            schema=TranscriptionResult, # <--- Expect a single result object
            key=api_key,
            temperature=0,
        )

        json_response_text = await response.text()

        # Parse the single JSON object and format it for the user
        final_output_message = ""
        try:
            clean_json_text = json_response_text.strip().removeprefix("```json").removesuffix("```").strip()
            # The entire data blob is our result object
            result_data = json.loads(clean_json_text)
            result = TranscriptionResult.model_validate(result_data)

            output_parts = []
            if result.transcription:
                # output_parts.append(f"**Transcription:**\n{result.transcription}")
                output_parts.append(f"{result.transcription}")

            if result.visual_description:
                output_parts.append(f"\n**Visuals:**\n{result.visual_description}")

            if not output_parts:
                message = result.error_message or "[No speech or text detected]"
                output_parts.append(f"_{message}_")

            final_output_message = "\n\n".join(output_parts)

        except (json.JSONDecodeError, Exception) as parse_error:
            print(f"Error parsing model's JSON response: {parse_error}")
            final_output_message = f"**Could not parse structured response, showing raw output:**\n\n`{json_response_text}`"

        final_output_message = final_output_message or "_No content was generated._"
        await status_message.delete()
        await util.discreet_send(event, final_output_message, link_preview=False, reply_to=event.message)

        if log:
            try:
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
                    f"{json_response_text}"
                )

                log_dir = os.path.expanduser(f"~/.borg/stt/log/{user_id}")
                os.makedirs(log_dir, exist_ok=True)
                log_file_path = os.path.join(log_dir, log_filename)

                with open(log_file_path, "w", encoding="utf-8") as f:
                    f.write(log_content)

            except Exception as log_e:
                print(f"Failed to write transcription log: {log_e}")
                print(traceback.format_exc())

    except Exception as e:
        print(e)
        print(traceback.format_exc())
        if "api key not valid" in str(e).lower():
            await status_message.delete()
            await request_api_key(event)
        else:
            await status_message.edit(f"An error occurred during the API call.")

# --- Bot Command Setup ---

async def set_bot_menu_commands():
    """
    Sets the bot's command menu in Telegram's UI.
    This should be called once after the client has started.
    """
    print("STT: setting bot commands ...")
    try:
        await asyncio.sleep(5)
        await borg(SetBotCommandsRequest(
            scope=BotCommandScopeDefault(),
            lang_code='en',
            commands=[
                BotCommand('start', 'Onboard and set API key'),
                BotCommand('help', 'Show help and instructions'),
                BotCommand('setgeminikey', 'Set or update your Gemini API key')
            ]
        ))
        print("Bot command menu has been updated.")
    except Exception as e:
        print(f"Failed to set bot commands: {e}")

# --- Telethon Event Handlers ---

PROCESSED_GROUP_IDS = set()

@borg.on(events.NewMessage(pattern="/start", func=lambda e: e.is_private))
async def start_handler(event):
    """
    Handles the /start command in private messages to onboard new users.
    """
    user_id = event.sender_id
    if user_id in AWAITING_KEY_FROM_USERS:
        cancel_key_flow(user_id)
        await event.reply("API key setup cancelled.")

    api_key = get_api_key(user_id=user_id, service="gemini")
    if api_key:
        await event.reply(
            "Welcome back! Your Gemini API key is already configured. "
            "You can send me an audio or video file to transcribe."
        )
    else:
        await request_api_key(event)

@borg.on(events.NewMessage(pattern="/help"))
async def help_handler(event):
    """Provides help information about the bot."""
    user_id = event.sender_id
    if user_id in AWAITING_KEY_FROM_USERS:
        cancel_key_flow(user_id)
        await event.reply("API key setup cancelled.")

    help_text = """
**Hello! I am a transcription bot powered by Google's Gemini.**

Here's how to use me:

1.  **Get a Gemini API Key:**
    You need a free API key to use my services. You can get one from Google AI Studio:
    ➡️ **https://aistudio.google.com/app/apikey**

2.  **Set Your API Key:**
    Use the /setGeminiKey command to save your key.
    - You can provide the key directly: `/setGeminiKey YOUR_API_KEY`
    - Or, just type /setGeminiKey and I will guide you through the setup.

3.  **Transcribe Media:**
    Simply send me any audio file, voice message, or video. I will transcribe the speech for you. If you send multiple files together (as an album), I will process them as a single request.

**Available Commands:**
- `/start`: Onboard and set up your API key for the first time.
- `/help`: Shows this help message.
- `/setGeminiKey [API_KEY]`: Sets or updates your Gemini API key.
"""
    await event.reply(help_text, link_preview=False)

@borg.on(events.NewMessage(pattern=r"(?i)/setGeminiKey(?:\s+(.*))?"))
async def set_key_handler(event):
    """
    Handles the /setGeminiKey command. Saves the user's API key if provided,
    otherwise initiates the interactive key setting flow.
    """
    api_key_match = event.pattern_match.group(1)
    user_id = event.sender_id

    if api_key_match and api_key_match.strip():
        api_key = api_key_match.strip()
        if not re.match(GEMINI_API_KEY_REGEX, api_key):
            await event.reply("The provided API key has an invalid format. Please check and try again.")
            return

        set_api_key(user_id=user_id, service="gemini", key=api_key)
        cancel_key_flow(user_id)

        await event.delete()
        confirmation_message = "✅ Your Gemini API key has been saved. Your message was deleted for security."
        try:
            await borg.send_message(user_id, confirmation_message)
            if not event.is_private:
                await event.reply("I've confirmed your key update in a private message.")
        except Exception:
            await event.respond(confirmation_message)
    else:
        await request_api_key(event)

@borg.on(events.NewMessage(incoming=True, func=lambda e: e.is_private and e.sender_id in AWAITING_KEY_FROM_USERS and not e.text.startswith('/')))
async def key_submission_handler(event):
    """
    Handles a plain-text message from a user who has been prompted for their API key.
    """
    user_id = event.sender_id
    text = event.text.strip()

    if text.lower() == 'cancel':
        cancel_key_flow(user_id)
        await event.reply("API key setup has been cancelled. You can start again with /setgeminikey.")
        return

    if not re.match(GEMINI_API_KEY_REGEX, text):
        API_KEY_ATTEMPTS[user_id] = API_KEY_ATTEMPTS.get(user_id, 0) + 1
        if API_KEY_ATTEMPTS[user_id] >= 3:
            cancel_key_flow(user_id)
            await event.reply("Too many invalid attempts. The API key setup has been cancelled. You can try again later with /setgeminikey.")
        else:
            remaining = 3 - API_KEY_ATTEMPTS[user_id]
            await event.reply(f"This does not look like a valid API key. Please try again. You have {remaining} attempt(s) left.")
        return

    set_api_key(user_id=user_id, service="gemini", key=text)
    cancel_key_flow(user_id)

    await event.delete()
    await event.respond(
        "✅ Your Gemini API key has been saved. Your message was deleted for security.\n"
        "You can now send an audio or video file to transcribe."
    )

@borg.on(events.NewMessage(func=lambda e: e.media is not None and e.sender and e.sender_id not in AWAITING_KEY_FROM_USERS))
async def media_handler(event):
    """
    Handles incoming messages with media, ensuring grouped media is processed only once.
    """
    user_id = event.sender_id
    if user_id in AWAITING_KEY_FROM_USERS:
        # If a user sends media while we're waiting for a key, we assume they want to transcribe,
        # so we cancel the key flow and proceed.
        cancel_key_flow(user_id)
        await event.reply("API key setup cancelled. Processing your media instead...")
        return

    group_id = event.grouped_id
    if group_id:
        if group_id in PROCESSED_GROUP_IDS:
            return # Already processing this group

        PROCESSED_GROUP_IDS.add(group_id)
        try:
            # util.run_and_upload is assumed to handle downloading files from the event
            # into a temporary directory `cwd` and passing it to the awaited function.
            await util.run_and_upload(event=event, to_await=llm_stt)
        finally:
            await asyncio.sleep(5) # Give some grace time for all messages to be processed
            PROCESSED_GROUP_IDS.remove(group_id)
    else:
        await util.run_and_upload(event=event, to_await=llm_stt)

# Schedule the command menu setup to run on the bot's event loop upon loading.
borg.loop.create_task(set_bot_menu_commands())
