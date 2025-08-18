from pynight.common_icecream import ic
import asyncio
import json
import os
import shutil
import tempfile
import traceback
import uuid
from datetime import datetime
from pathlib import Path

from pydantic import BaseModel, Field
from telethon import events
from telethon.tl.types import KeyboardButtonCallback

from uniborg import bot_util, llm_db, llm_util, tts_util, util
from uniborg.storage import UserStorage

# --- Bot Configuration ---

STORAGE_PURPOSE = "tts_bot_preferences"

# Logging configuration
LOG_DIR = Path(os.path.expanduser("~/.borg/tts_bot/log/"))
LOG_DIR.mkdir(parents=True, exist_ok=True)
BOT_COMMANDS = [
    {"command": "start", "description": "Onboard and set API key"},
    {"command": "help", "description": "Show help and instructions"},
    {"command": "setgeminikey", "description": "Set or update your Gemini API key"},
    {"command": "geminivoice", "description": "Choose a default voice for TTS"},
    {"command": "model", "description": "Choose a default TTS model"},
]


# --- Preference Management ---


class UserPrefs(BaseModel):
    """Pydantic model for type-safe user preferences for the TTS Bot."""

    voice: str = Field(default=tts_util.DEFAULT_VOICE)
    model: str = Field(default="gemini-2.5-flash-preview-tts")


class UserManager:
    """High-level manager for user preferences."""

    def __init__(self):
        self.storage = UserStorage(purpose=STORAGE_PURPOSE)

    def get_prefs(self, user_id: int) -> UserPrefs:
        data = self.storage.get(user_id)
        return UserPrefs.model_validate(data or {})

    def set_voice(self, user_id: int, voice: str):
        prefs = self.get_prefs(user_id)
        prefs.voice = voice
        self.storage.set(user_id, prefs.model_dump())

    def set_model(self, user_id: int, model: str):
        prefs = self.get_prefs(user_id)
        prefs.model = model
        self.storage.set(user_id, prefs.model_dump())


user_manager = UserManager()


# --- Core Logic ---


async def _get_text_from_files(
    all_messages: list, temp_dir: Path
) -> tuple[str, list[str]]:
    """
    Downloads media from a list of messages, filters for text files, and extracts their content.
    Returns the combined text and a list of warnings for ignored files.
    """
    text_content_parts = []
    warnings = []
    text_extensions = {".txt", ".md", ".py", ".json", ".xml", ".log", ".csv"}

    for message in all_messages:
        if not message.media:
            continue
        try:
            file_path_str = await message.download_media(file=temp_dir)
            if not file_path_str:
                continue
            file_path = Path(file_path_str)
            filename = file_path.name
            if file_path.suffix.lower() in text_extensions:
                file_text = file_path.read_text(encoding="utf-8")
                text_content_parts.append(
                    f"File: {filename}\n``````\n{file_text}\n``````"
                )
            else:
                warnings.append(f"Ignored non-text file: `{filename}`")
        except Exception as e:
            warnings.append(f"Could not process a media file: {e}")

    return "\n\n".join(text_content_parts), warnings


async def _log_tts_generation(
    event, prefs: UserPrefs, text_input: str, audio_file_generated: bool
):
    """Formats and writes the TTS generation log to a user-specific file."""
    try:
        user = await event.get_sender()
        user_id = user.id
        first_name = user.first_name or ""
        last_name = user.last_name or ""
        username = user.username or "N/A"
        full_name = f"{first_name} {last_name}".strip()

        timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        unique_id = uuid.uuid4().hex
        log_filename = f"{timestamp}_{unique_id}.txt"

        user_log_dir = LOG_DIR / str(user_id)
        user_log_dir.mkdir(exist_ok=True)
        log_file_path = user_log_dir / log_filename

        # Log prefs object as JSON
        prefs_dict = prefs.model_dump()
        prefs_json = json.dumps(prefs_dict, indent=2)

        log_parts = [
            f"Date: {timestamp}",
            f"User ID: {user_id}",
            f"Name: {full_name}",
            f"Username: @{username}",
            "--- Preferences ---",
            prefs_json,
            "--- TTS Generation ---",
            f"\n[User Text Input]:",
            text_input,
            f"\n[Generation Result]:",
            f"Audio file generated: {'Yes' if audio_file_generated else 'No'}",
        ]

        with open(log_file_path, "w", encoding="utf-8") as f:
            f.write("\n".join(log_parts))
    except Exception as e:
        print(f"Failed to write TTS generation log for user {event.sender_id}: {e}")
        traceback.print_exc()


# --- Event and Command Handlers ---


async def message_handler(event, *, log_p=True):
    """Main handler for generating TTS from user messages and files."""
    if not event.text:
        #: This skips messages in a group that are file-only.
        #: It also conveniently skips file-only messages in general, which will allow the user to reply to them and specify their instructions and style.
        return

    if event.text.startswith("/"):
        return
    if llm_db.is_awaiting_key(event.sender_id):
        return

    api_key = llm_db.get_api_key(user_id=event.sender_id, service="gemini")
    if not api_key:
        await llm_db.request_api_key_message(event, "gemini")
        return

    group_id = event.grouped_id
    if group_id:
        if group_id in bot_util.PROCESSED_GROUP_IDS:
            return
        bot_util.PROCESSED_GROUP_IDS.add(group_id)
        await asyncio.sleep(0.2)

    status_message = await event.reply("Processing...")
    import tempfile
    temp_dir = Path(tempfile.gettempdir()) / f"temp_tts_bot_{event.id}"
    ogg_file_path = None

    try:
        temp_dir.mkdir(exist_ok=True)

        # Build the list of initial messages by walking up the entire reply chain
        initial_messages = [event.message]
        if event.is_reply:
            try:
                message = event.message
                while message and message.reply_to_msg_id:
                    message = await event.client.get_messages(
                        event.chat_id, ids=message.reply_to_msg_id
                    )
                    if message:
                        initial_messages.append(message)
                    else:
                        break
            except Exception as e:
                print(f"TTS Bot: Could not fetch full reply chain: {e}")

        # Expand to include all messages in media groups and sort chronologically
        all_messages_to_process = await bot_util.expand_and_sort_messages_with_groups(
            event, initial_messages
        )

        # Start with the current message's text (the instructions)
        final_text = event.text

        # Append text from all other messages in chronological order
        past_messages_text = [
            msg.text
            for msg in all_messages_to_process
            if msg.id != event.id and msg.text and not msg.text.startswith("/")
        ]
        if past_messages_text:
            final_text += "\n\n" + "\n\n".join(past_messages_text)

        # Extract text from any attached text files from all messages and append it
        file_text, warnings = await _get_text_from_files(
            all_messages_to_process, temp_dir
        )
        if file_text:
            final_text = f"{final_text}\n\n{file_text}".strip()

        if not final_text.strip():
            await status_message.edit("No text found in your message or text files.")
            if warnings:
                await event.reply(
                    f"**Note:**\n" + "\n".join(f"- {w}" for w in warnings)
                )
            return

        await status_message.edit("Generating audio...")
        user_prefs = user_manager.get_prefs(event.sender_id)
        ogg_file_path = await tts_util.generate_tts_audio(
            text=final_text,
            voice=user_prefs.voice,
            model=user_prefs.model,
            api_key=api_key,
            template_mode=False,
        )

        await event.client.send_file(
            event.chat_id, ogg_file_path, voice_note=True, reply_to=event.id
        )
        await status_message.delete()
        if warnings:
            await event.reply(f"**Note:**\n" + "\n".join(f"- {w}" for w in warnings))

        # Log the interaction if logging is enabled
        if log_p:
            await _log_tts_generation(event, user_prefs, final_text, True)

    except Exception as e:
        await status_message.delete()
        await tts_util.handle_tts_error(event=event, exception=e, service="gemini")
        traceback.print_exc()
    finally:
        if group_id:
            bot_util.PROCESSED_GROUP_IDS.discard(group_id)
        if ogg_file_path and os.path.exists(ogg_file_path):
            await util.async_remove_file(ogg_file_path)
        if temp_dir.exists():
            await util.async_remove_dir(str(temp_dir))


async def help_handler(event):
    """Provides help information."""
    help_text = """
**TTS Bot Help**

I convert text into high-quality speech using Google's Gemini models.

**How to Use:**
1.  **Set API Key:** You need a free Gemini API key. Use /setgeminikey to add yours.
2.  **Send Text:** Send me any message, and I'll read it back to you.
3.  **Attach Files:** You can also attach text files (`.txt`, `.md`, etc.). I will read the text from your message first, then the content of each file.
4.  **Choose a Voice:** Use the /geminivoice command to select from a variety of voices.

**Commands:**
- `/start`: Set up your API key.
- `/help`: Show this message.
- `/setgeminikey`: Add or update your Gemini API key.
- `/geminivoice`: Choose a default TTS voice.
- `/model`: Choose a default TTS model.
"""
    await event.reply(help_text, link_preview=False)


async def set_key_handler(event):
    """Delegates to the shared API key setting command."""
    await llm_db.handle_set_key_command(event, "gemini")


async def key_submission_handler(event):
    """Delegates plain-text key submission to the shared handler."""
    await llm_db.handle_key_submission(
        event, success_msg="You can now use the TTS bot."
    )


async def gemini_voice_handler(event):
    """Presents the voice selection menu."""
    current_voice = user_manager.get_prefs(event.sender_id).voice
    voice_options = {
        name: f"{name}: {desc}" for name, desc in tts_util.GEMINI_VOICES.items()
    }
    await bot_util.present_options(
        event,
        title="**Choose a TTS Voice**",
        options=voice_options,
        current_value=current_voice,
        callback_prefix="voice_",
        awaiting_key="voice_selection",
        n_cols=3,
    )


async def gemini_model_handler(event):
    """Presents the TTS model selection menu."""
    current_model = user_manager.get_prefs(event.sender_id).model
    model_options = {model: desc for model, desc in tts_util.TTS_MODELS.items()}
    await bot_util.present_options(
        event,
        title="**Choose a TTS Model**",
        options=model_options,
        current_value=current_model,
        callback_prefix="model_",
        awaiting_key="model_selection",
        n_cols=2,
    )


async def voice_callback_handler(event):
    """Handles the user's voice selection from the inline keyboard."""
    voice = event.data.decode("utf-8").split("_", 1)[1]
    user_manager.set_voice(event.sender_id, voice)
    buttons = [
        KeyboardButtonCallback(
            f"✅ {name}: {desc}" if name == voice else f"{name}: {desc}",
            data=f"voice_{name}",
        )
        for name, desc in tts_util.GEMINI_VOICES.items()
    ]
    try:
        await event.edit(buttons=util.build_menu(buttons, n_cols=3))
    except Exception:
        pass
    await event.answer(f"Voice set to {voice}")


async def model_callback_handler(event):
    """Handles the user's model selection from the inline keyboard."""
    model = event.data.decode("utf-8").split("_", 1)[1]
    user_manager.set_model(event.sender_id, model)
    buttons = [
        KeyboardButtonCallback(
            f"✅ {model}: {desc}" if model == model else f"{model}: {desc}",
            data=f"model_{model}",
        )
        for model, desc in tts_util.TTS_MODELS.items()
    ]
    try:
        await event.edit(buttons=util.build_menu(buttons, n_cols=2))
    except Exception:
        pass
    await event.answer(f"Model set to {model}")


# --- Initialization ---


def register_handlers():
    """Registers all event handlers for this plugin."""
    # Create start handler from the shared utility
    start_handler = llm_util.create_llm_start_handler(
        service="gemini",
        welcome_message=(
            "Hey! I turn your text (and attached text files) into speech using "
            "Google Gemini.\n\n"
            "• First, add your Gemini API key with /setgeminikey\n"
            "• Then send me any text or attach text files—I’ll reply with a voice note\n"
            "• Use /geminivoice to pick a default voice\n"
            "• Need details? /help"
        ),
        configured_message=(
            "You're all set; your Gemini API key is already saved.\n\n"
            "Send me text or attach text files and I’ll generate a voice note. "
            "Change the default voice anytime with /geminiVoice. For tips and commands, see /help."
        ),
    )

    # Register all handlers
    borg.on(events.NewMessage(pattern=r"(?i)^/start\s*$", func=lambda e: e.is_private))(
        start_handler
    )
    borg.on(events.NewMessage(pattern=r"(?i)^/help\s*$", func=lambda e: e.is_private))(
        help_handler
    )
    borg.on(
        events.NewMessage(
            pattern=r"(?i)/setgeminikey(?:\s+(.*))?\s*$", func=lambda e: e.is_private
        )
    )(set_key_handler)
    borg.on(
        events.NewMessage(
            func=lambda e: e.is_private
            and llm_db.is_awaiting_key(e.sender_id)
            and not e.text.startswith("/")
        )
    )(key_submission_handler)
    borg.on(
        events.NewMessage(pattern=r"(?i)^/geminivoice\s*$", func=lambda e: e.is_private)
    )(gemini_voice_handler)
    borg.on(events.NewMessage(pattern=r"(?i)^/model\s*$", func=lambda e: e.is_private))(
        gemini_model_handler
    )
    borg.on(events.CallbackQuery(pattern=b"voice_"))(voice_callback_handler)
    borg.on(events.CallbackQuery(pattern=b"model_"))(model_callback_handler)
    borg.on(
        events.NewMessage(
            func=lambda e: e.is_private and (e.text or e.media) and not e.forward
        )
    )(message_handler)


async def initialize_tts_bot():
    """Initializes the bot by registering commands and handlers."""
    await bot_util.register_bot_commands(borg, BOT_COMMANDS)
    register_handlers()


# Schedule the initialization to run on the bot's event loop
borg.loop.create_task(initialize_tts_bot())
