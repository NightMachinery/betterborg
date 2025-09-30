from icecream import ic
from uniborg import util
from uniborg import llm_util
from uniborg import llm_db
from uniborg.constants import GEMINI_FLASH_LATEST
import os
import traceback
import llm
import uuid
import asyncio
import json
from datetime import datetime
from pathlib import Path
from telethon import events
from telethon.tl.functions.bots import SetBotCommandsRequest
from telethon.tl.types import BotCommand, BotCommandScopeDefault
from pydantic import BaseModel, Field
from typing import Optional

# --- Bot Commands Registration ---
BOT_COMMANDS = [
    {"command": "start", "description": "Onboard and set API key"},
    {"command": "help", "description": "Show help and instructions"},
    {"command": "setgeminikey", "description": "Set or update your Gemini API key"},
]

# --- Pydantic Schema and Prompt for Transcription ---


class TranscriptionResult(BaseModel):
    """The synthesized processing result for ALL provided media files."""

    transcription: str = Field(
        description="The combined verbatim audio transcription or OCR text from all files. Separate content from different files with '---'. Empty if no speech/text is found."
    )
    visual_description: Optional[str] = Field(
        None,
        description="For video(s) ONLY, a combined narrative of the key visual scenes from all videos. MUST be null otherwise.",
    )
    output_type: str = Field(
        "none",
        description="The dominant output type. One of: 'transcript' (if any audio/video), 'ocr' (if only images), or 'none'.",
    )
    error_message: Optional[str] = Field(
        None,
        description="If processing failed entirely, provide a brief error message here.",
    )


# --- New System Prompt for a Single, Synthesized JSON Output ---

TRANSCRIPTION_PROMPT_V6 = r"""
Your mission is to act as a media processing engine. Analyze ALL attached media files and synthesize their content into a SINGLE structured JSON object.

Your entire output MUST be a single, valid JSON object that conforms to the `TranscriptionResult` schema provided.

Follow these synthesis rules:
1.  **`transcription` field**:
    - For all files, combine their meaningful content into this single field.
    - **Content Rules:**
        - **Language:**
            - **Farsi/English:** Transcribe these languages accurately as you hear them.
            - **Other Languages & Translation:** If you are SURE the language is not Farsi or English, you **MUST** provide two things:
                1. The transcription in the original language.
                2. The English translation of that transcription. Separate the translation from the original using clear formatting.
            - **CRITICAL:** You **MUST NOT** translate Farsi transcriptions.
        - **Inclusion:** Transcribe spoken words from audio/video, formatted lyrics from songs (if they are the primary content, not background music), and text from images (OCR).
        - **Exclusion:** Skip filler words (um, uh, er), false starts, repetitions, non-speech sounds (music/effects if speech is present), and discourse markers (well, I mean). Omit words when in doubt.
        - **Formatting:**
            - **Readability:** Use standard punctuation (commas, periods, new lines) and create new paragraphs for different topics or speakers to make the text easy to read. Maintain the spatial structure of the text when doing OCR or transcribing lyrics or dialogue using appropriate whitespace etc. You can use custom markdown markup: `**bold**`, `` `code` ``, or `__italic__` are available. In addition you can send `[links](https://example.com)` and ```` ```pre``` ```` blocks with three backticks.

            - **Emoji Use in Audio/Video Transcription:** use emojis liberally to reflect the body language, tone and emotions of the speakers. Be especially generous when romance is involved! (This directive does NOT apply when doing OCR on images.)

            - **Speaker Identification:** When MULTIPLE people are speaking, each speaker label must be on its own line (after a line break) in bold, followed by a colon, then their dialogue (either on the same line or after a line break, depending on your own judgement). Use the original language of the dialogue for all labels.

              Choose appropriate labels based on context:
              • Person's name: "**María:**", "**سپیده:**"
              • Job title or role: "**Detective:**", "**آرایشگر:**"
              • Generic labels when unsure: "**Speaker 1:**", "**گوینده ۱:**"

              **CRITICAL:** All labels must match the language being spoken, even when guessing names or roles. For Persian/Farsi dialogue, you MUST use matching Persian labels.

            - **Separators:** If you process multiple files, you MUST place `---` on its own line to separate the content from each distinct file.

            - When in doubt, use more whitespace, new lines (line breaks), and new paragraphs. This improves readability and reduces clutter.

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
print(f"STT Prompt Loaded:\n\n{TRANSCRIPTION_PROMPT}\n---\n\n")


# --- Core Transcription Logic ---


async def llm_stt(
    *, cwd, event, model_name=GEMINI_FLASH_LATEST, log=True
):
    """
    Performs speech-to-text on media, enforcing a single structured JSON output
    that synthesizes all provided files.
    """
    parse_mode = "md"
    italics_marker = "__"

    api_key = llm_db.get_api_key(user_id=event.sender_id, service="gemini")
    if not api_key:
        await llm_db.request_api_key_message(event, "gemini")
        return

    try:
        model = llm.get_async_model(model_name)
        if not getattr(model, "supports_schema", False):
            await event.reply(
                f"Error: The model '{model_name}' does not support structured output (schemas)."
            )
            return
    except llm.UnknownModelError:
        await event.reply(
            f"Error: '{model_name}' model not found. Perhaps the relevant LLM plugin has not been installed."
        )
        return
    except Exception as e:
        await llm_util.handle_llm_error(
            event=event,
            exception=e,
            base_error_message="An unexpected error occurred while loading the model.",
            error_id_p=True,
        )
        return

    # --- Refactored Attachment Creation ---
    # The complex logic of iterating files, checking MIME types, and reading
    # content is now handled by the centralized utility function.
    attachments = llm_util.create_attachments_from_dir(Path(cwd))

    if not attachments:
        await event.reply("No valid media files found to transcribe.")
        return

    status_message = await event.reply("Transcribing...")

    json_response_text = None
    try:
        # Pass the single TranscriptionResult schema directly
        response = await model.prompt(
            prompt=TRANSCRIPTION_PROMPT,
            attachments=attachments,
            schema=TranscriptionResult,
            key=api_key,
            temperature=0,
        )

        json_response_text = await response.text()

        # Parse the single JSON object and format it for the user
        final_output_message = ""
        try:
            clean_json_text = (
                json_response_text.strip()
                .removeprefix("```json")
                .removesuffix("```")
                .strip()
            )
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
                output_parts.append(f"{italics_marker}{message}{italics_marker}")

            final_output_message = "\n\n".join(output_parts)

        except (json.JSONDecodeError, Exception) as parse_error:
            print(f"Error parsing model's JSON response: {parse_error}")
            final_output_message = f"**Could not parse structured response, showing raw output:**\n\n```json\n{json_response_text}\n```"

        final_output_message = (
            final_output_message
            or "{italics_marker}No content was generated.{italics_marker}"
        )
        await util.discreet_send(
            event,
            final_output_message,
            link_preview=False,
            reply_to=event.message,
            parse_mode=parse_mode,
        )

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

                log_content += f"--- Transcription ---\n" f"{json_response_text}"

                log_dir = os.path.expanduser(f"~/.borg/stt/log/{user_id}")
                os.makedirs(log_dir, exist_ok=True)
                log_file_path = os.path.join(log_dir, log_filename)

                with open(log_file_path, "w", encoding="utf-8") as f:
                    f.write(log_content)

            except Exception as log_e:
                print(f"Failed to write transcription log: {log_e}")
                print(traceback.format_exc())

        await status_message.delete()

    except Exception as e:
        await llm_util.handle_llm_error(
            event=event,
            exception=e,
            response_message=status_message,
            service="gemini",
            base_error_message="An error occurred during the API call.",
            error_id_p=True,
        )


# --- Bot Command Setup ---


async def set_bot_menu_commands():
    """
    Sets the bot's command menu in Telegram's UI.
    This should be called once after the client has started.
    """
    print("STT: setting bot commands ...")
    try:
        await asyncio.sleep(5)
        await borg(
            SetBotCommandsRequest(
                scope=BotCommandScopeDefault(),
                lang_code="en",
                commands=[
                    BotCommand(c["command"], c["description"]) for c in BOT_COMMANDS
                ],
            )
        )
        print("STT: Bot command menu has been updated.")
    except Exception as e:
        print(f"STT: Failed to set bot commands: {e}")


# --- Telethon Event Handlers ---

PROCESSED_GROUP_IDS = set()


@borg.on(events.NewMessage(pattern="/start", func=lambda e: e.is_private))
async def start_handler(event):
    """Handles the /start command to onboard new users."""
    user_id = event.sender_id
    if llm_db.is_awaiting_key(user_id):
        llm_db.cancel_key_flow(user_id)
    if llm_db.get_api_key(user_id=user_id, service="gemini"):
        await event.reply(
            "Welcome back! Your Gemini API key is already configured. You can send me media files to transcribe."
        )
    else:
        await llm_db.request_api_key_message(event, "gemini")


@borg.on(events.NewMessage(pattern="/help"))
async def help_handler(event):
    """Provides help information."""
    if llm_db.is_awaiting_key(event.sender_id):
        llm_db.cancel_key_flow(event.sender_id)
        await event.reply("API key setup cancelled.")

    help_text = """
**Hello! I am a transcription bot powered by Google's Gemini.**

Here's how to use me:

1.  **Get a Gemini API Key:**
    You need a free API key to use my services. Get one from Google AI Studio:
    ➡️ **https://aistudio.google.com/app/apikey**

2.  **Set Your API Key:**
    Use the /setGeminiKey command to save your key.
    - Provide the key directly: `/setGeminiKey YOUR_API_KEY`
    - Or, just type /setGeminiKey and I will guide you.
3.  **Transcribe Media:**
    Simply send any audio file, voice message, or video. If you send multiple files as an album, I will process them in a single request.
**Available Commands:**
- `/start`: Onboard and set up your API key.
- `/help`: Shows this help message.
- `/setGeminiKey [API_KEY]`: Sets or updates your Gemini API key.
"""
    await event.reply(help_text, link_preview=False)


@borg.on(events.NewMessage(pattern=r"(?i)/setGeminiKey(?:\s+(.*))?"))
async def set_key_handler(event):
    """Delegates /setgeminikey command logic to the shared module."""
    await llm_db.handle_set_key_command(event)


@borg.on(
    events.NewMessage(
        func=lambda e: e.is_private
        and llm_db.is_awaiting_key(e.sender_id)
        and not e.text.startswith("/")
    )
)
async def key_submission_handler(event):
    """Delegates plain-text key submission logic to the shared module."""
    await llm_db.handle_key_submission(event)


@borg.on(events.NewMessage(func=lambda e: e.media is not None and e.sender))
async def media_handler(event):
    """
    Handles incoming messages with media. If the user is being prompted for an
    API key, this will cancel the prompt and attempt to process the media.
    """
    user_id = event.sender_id

    # If user sends media while being prompted for a key, cancel the flow.
    if llm_db.is_awaiting_key(user_id):
        llm_db.cancel_key_flow(user_id)
        await event.reply("API key setup cancelled. Processing your media instead...")

    group_id = event.grouped_id
    if group_id:
        if group_id in PROCESSED_GROUP_IDS:
            return  # Already processing this group

        PROCESSED_GROUP_IDS.add(group_id)
        try:
            # util.run_and_upload is assumed to handle downloading files from the event
            # into a temporary directory `cwd` and passing it to the awaited function.
            await util.run_and_upload(event=event, to_await=llm_stt)
        finally:
            await asyncio.sleep(
                5
            )  # Give some grace time for all messages to be processed
            PROCESSED_GROUP_IDS.remove(group_id)
    else:
        await util.run_and_upload(event=event, to_await=llm_stt)


# --- Initialization ---
# Schedule the command menu setup to run on the bot's event loop upon loading.
borg.loop.create_task(set_bot_menu_commands())
