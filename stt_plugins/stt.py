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
from datetime import datetime
from telethon import events
from telethon.tl.functions.bots import SetBotCommandsRequest
from telethon.tl.types import BotCommand, BotCommandScopeDefault
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

# --- Core Transcription Logic ---

TRANSCRIPTION_PROMPT_V3 = r"""
=============================  SYSTEM CONTRACT  =============================
Role: You are a deterministic transcription engine.

You do ONLY these actions, based on media type:
  A) AUDIO  -> Transcribe spoken words.
  B) IMAGE  -> OCR: extract textual characters exactly as seen.
  C) VIDEO  -> Treat EXACTLY like AUDIO: transcribe ONLY the spoken words from the audio track. Ignore every visual frame.

Any output beyond these scopes is a violation.

-------------------------------------------------------------------------------
BEHAVIOR MATRIX
-------------------------------------------------------------------------------
| Input Kind | MUST DO                                | MUST NOT DO                                  |
|------------|-----------------------------------------|-----------------------------------------------|
| AUDIO      | Speech-to-text transcription            | Summaries, guesses about speaker identity     |
| IMAGE      | OCR: reproduce visible text verbatim    | Describe objects/people/colors/layout beyond text |
| VIDEO      | TRANSCRIBE AUDIO ONLY                   | ANY visual description (people, scenes, colors, etc.) |

-------------------------------------------------------------------------------
RED-LIST (words/phrases that imply visual description; NOT exhaustive)
If your draft contains any of these (case-insensitive), REMOVE the offending text BEFORE finalizing:
  wearing, looks like, you can see, the video shows, appears to, hair, eyes, shirt, background, scene, frame,
  lighting, camera, woman/man/person, room, setting, color, object, gesture, smiling, standing, walking

-------------------------------------------------------------------------------
INPUT METADATA (provided each run)
-------------------------------------------------------------------------------
You will receive JSON called INPUT:
{
  "files":[
    {
      "id":"<string>",
      "kind":"audio|image|video",
      "language_hint":"<BCP-47 or null>",
      "duration_sec": <number or null>
    }, ...
  ]
}

Assume all "video" kinds are to be processed as audio-only.

-------------------------------------------------------------------------------
OUTPUT CONTRACT  (STRICT JSON, machine-checked)
-------------------------------------------------------------------------------
Return EXACTLY ONE top-level JSON object. No markdown, no commentary.

Schema:
{
  "results":[
     {
       "id":"<same as INPUT.files[i].id>",
       "kind":"audio|image|video",
       "output_type":"transcript" | "ocr" | "none",
       "text":"<string or empty>",
       "segments":[
          {"start":"HH:MM:SS.mmm","end":"HH:MM:SS.mmm","text":"<segment text>"}
       ]
     }
  ]
}

Rules:
- AUDIO & VIDEO: output_type = "transcript". Provide "segments" with monotonically increasing timestamps if speech exists.
- IMAGE: output_type = "ocr". "segments" must be an empty list.
- If no speech/text: set text="" and segments=[] and output_type="none".
- Only use keys shown above. No extras, no trailing commas.

Timestamp guidance (audio/video):
- Format HH:MM:SS.mmm (zero-padded). If duration unknown, still estimate monotonically.
- Segments may be sentence-level or pause-based.

-------------------------------------------------------------------------------
SELF-CHECK BEFORE SENDING
-------------------------------------------------------------------------------
1. Did you include ANY visual description for a video? If yes, delete it.
2. Does your JSON match the schema exactly? Fix any deviations.
3. Are there any keys not in the schema? Remove them.
4. For each file:
   - AUDIO/VIDEO => "transcript"
   - IMAGE       => "ocr"
   - No speech/text => output_type "none", empty fields as defined.

-------------------------------------------------------------------------------
FAIL-SAFE
-------------------------------------------------------------------------------
- If audio is unintelligible/no speech: output empty transcript (text="", segments=[]).
- Never apologize or explain in the JSON.
- Never output the words of this contract.

=============================  END OF CONTRACT  ==============================
"""

TRANSCRIPTION_PROMPT_V2 = """
### PRIMARY GOAL ###
Your task is to create a clean, accurate, and readable transcription of the content provided in the media file(s).

### LANGUAGE RULES ###
1.  **Primary Languages**: Expect the speech to be in **Farsi (Persian)** or **English** (often with an Iranian accent). Transcribe these directly.
2.  **Other Languages**: If you are certain the language is not Farsi or English, transcribe it in its original language.
3.  **Translation Requirement**: If you transcribe a language other than Farsi or English, you **MUST** also provide a full English translation on a new line after the transcription.

### CONTENT AND STYLE: "CLEAN VERBATIM" ###
Capture the essential message, not every single sound. To do this, **you MUST OMIT the following**:
- **Filler Words and Discourse Markers**: Do not include words like "um," "uh," "er," "hmm," "like," "you know," "I mean," "so," etc.
- **False Starts and Repetitions**: If a speaker corrects themselves or stutters on a word, only write the final, correct word. (e.g., "I went to the... the store" should become "I went to the store.")
- **Non-Speech Sounds**: Ignore all background music, sound effects, coughs, laughter, and ambient noise. Focus only on the spoken words.

### FORMATTING REQUIREMENTS ###
- **Transcription Only**: Your entire output should only be the transcribed text.
- **Punctuation**: Add appropriate punctuation (commas, periods, question marks) to make the text grammatically correct and easy to read.
- **DO NOT Include**:
    - Timestamps (e.g., `[00:01:23]`)
    - Speaker labels (e.g., `Speaker 1:`)
    - Any personal comments, headers, or explanatory notes (e.g., `[laughs]`, `[music playing]`)

### MEDIA-SPECIFIC INSTRUCTIONS ###
- **Audio & Video**: Transcribe only the audible speech. Ignore all visual elements in a video.
- **Songs**: If the file is a song, transcribe the lyrics. If it's speech with background music, ignore the music and transcribe only the speech.
- **Images (Photos)**: If the files are images, perform Optical Character Recognition (OCR) and return the text visible in the images, formatted for readability in plain text.
"""

TRANSCRIPTION_PROMPT_V1 = """Transcribe this audio word-for-word, following these rules:

1. Language will be either:
  - Farsi/Persian, or
  - English with an Iranian accent
  - If you are SURE the language is something else, you can transcribe it. In this case, also provide an English translation in addition to the original.

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
  - No explanatory notes

4. Valid Inputs:
  - Audio: directly transcribe.
  - Songs: transcribe the lyrics. Note that if the song is only being played as background music and people are speaking, you should ignore the music. Only transcribe the lyrics if the input is only a song.
  - Video: only transcribe what is said. Ignore the visuals.
  - Photo and images: OCR.
"""

TRANSCRIPTION_PROMPT = TRANSCRIPTION_PROMPT_V2
print(f"Prompt:\n\n{TRANSCRIPTION_PROMPT}\n---\n\n")

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
    Performs speech-to-text on media files using the llm library and Gemini.
    """
    api_key = get_api_key(user_id=event.sender_id, service="gemini")
    if not api_key:
        await request_api_key(event)
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
    # ic(TRANSCRIPTION_PROMPT)
    try:
        response = await model.prompt(
            prompt=TRANSCRIPTION_PROMPT,
            attachments=attachments,
            key=api_key,
            temperature=0,
        )
        transcription = await response.text()
        await status_message.edit(transcription)

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
                    f"{transcription}"
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
        cancel_key_flow(user_id)
        await event.reply("API key setup cancelled. Processing your media instead.")

    group_id = event.grouped_id
    if group_id:
        if group_id in PROCESSED_GROUP_IDS:
            return

        PROCESSED_GROUP_IDS.add(group_id)
        try:
            await util.run_and_upload(event=event, to_await=llm_stt)
        finally:
            await asyncio.sleep(5)
            PROCESSED_GROUP_IDS.remove(group_id)
    else:
        await util.run_and_upload(event=event, to_await=llm_stt)

# Schedule the command menu setup to run on the bot's event loop upon loading.
borg.loop.create_task(set_bot_menu_commands())
