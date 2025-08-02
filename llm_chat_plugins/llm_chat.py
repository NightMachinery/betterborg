import asyncio
import traceback
import os
import uuid
import base64
import mimetypes
from datetime import datetime
from pathlib import Path
from shutil import rmtree

import litellm
from telethon import events, errors
from telethon.tl.functions.bots import SetBotCommandsRequest
from telethon.tl.types import BotCommand, BotCommandScopeDefault
from pydantic import BaseModel, Field

# Import uniborg utilities and storage
from uniborg import util
from uniborg import llm_db
from uniborg.storage import UserStorage

# --- Constants and Configuration ---

# Use the litellm model naming convention.
# See https://docs.litellm.ai/docs/providers/gemini
DEFAULT_MODEL = "gemini/gemini-2.5-flash" #: Do NOT change the default model unless explicitly instructed to.
DEFAULT_SYSTEM_PROMPT = """
You are a helpful and knowledgeable assistant. Your primary audience is advanced STEM postgraduate researchers, so be precise and technically accurate.

**Style Guidelines for Mobile Chat:**
- **Concise & Direct:** Keep responses as brief as possible without sacrificing critical information. Get straight to the point. Exception: Provide full detail when users specifically request lengthy responses.
- **Conversational Tone:** Write in a clear, natural style suitable for a chat conversation. Avoid overly academic or verbose language unless necessary for technical accuracy. You can use emojis.
- **Readability:** Break up text into short paragraphs. Use bullet points or numbered lists to make complex information easy to scan on a small screen.

**Formatting:** You can use Telegram's markdown: `**bold**`, `__italic__`, `` `code` ``, `[links](https://example.com)`, and ```pre``` blocks.
"""

# Directory for logs, mirroring the STT plugin's structure
LOG_DIR = Path(os.path.expanduser("~/.borg/llm_chat/log/"))
LOG_DIR.mkdir(parents=True, exist_ok=True)


# --- User Preference Management ---

class UserPrefs(BaseModel):
    """Pydantic model for type-safe user preferences."""
    model: str = Field(default=DEFAULT_MODEL)
    system_prompt: str = Field(default=DEFAULT_SYSTEM_PROMPT)

class UserManager:
    """High-level manager for user preferences, using the UserStorage class."""
    def __init__(self):
        self.storage = UserStorage(purpose="llm_chat")

    def get_prefs(self, user_id: int) -> UserPrefs:
        data = self.storage.get(user_id)
        return UserPrefs.model_validate(data or {})

    def set_model(self, user_id: int, model_name: str):
        prefs = self.get_prefs(user_id)
        prefs.model = model_name
        self.storage.set(user_id, prefs.model_dump())

    def set_system_prompt(self, user_id: int, prompt: str):
        prefs = self.get_prefs(user_id)
        prefs.system_prompt = prompt
        self.storage.set(user_id, prefs.model_dump())

user_manager = UserManager()


# --- Core Logic & Helpers ---

async def _log_conversation(event, model_name: str, messages: list, final_response: str):
    """Formats and writes the conversation log to a user-specific file."""
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

        log_parts = [
            f"Date: {timestamp}",
            f"User ID: {user_id}",
            f"Name: {full_name}",
            f"Username: @{username}",
            f"Model: {model_name}",
            "--- Conversation ---"
        ]

        for msg in messages:
            role = msg.get("role", "unknown").capitalize()
            content = msg.get("content")

            log_parts.append(f"\n[{role}]:")
            if isinstance(content, str):
                log_parts.append(content)
            elif isinstance(content, list):
                # Handle multimodal content for logging
                for part in content:
                    if part.get("type") == "text":
                        log_parts.append(part.get("text", ""))
                    elif part.get("type") == "image_url":
                        log_parts.append("[Attachment: Image]")

        log_parts.append("\n[Assistant]:")
        log_parts.append(final_response)

        with open(log_file_path, "w", encoding="utf-8") as f:
            f.write("\n".join(log_parts))

    except Exception as e:
        print(f"Failed to write chat log for user {event.sender_id}: {e}")
        traceback.print_exc()


async def build_conversation_history(event) -> list:
    """
    Constructs a conversation history for litellm from the reply chain,
    downloading and encoding media. Excludes the current message.
    """
    if not event.message.reply_to_msg_id:
        return []

    history = []
    message = await event.client.get_messages(event.chat_id, ids=event.message.reply_to_msg_id)
    bot_me = await event.client.get_me()
    temp_dir = Path(f"./temp_llm_chat_history_{event.id}/")
    temp_dir.mkdir(exist_ok=True)

    messages_to_process = []
    while message:
        messages_to_process.append(message)
        if not message.reply_to_msg_id:
            break
        message = await event.client.get_messages(event.chat_id, ids=message.reply_to_msg_id)

    messages_to_process.reverse()  # Process from oldest to newest

    try:
        for msg in messages_to_process:
            role = "assistant" if msg.sender_id == bot_me.id else "user"
            text_content = msg.text or ""

            if not msg.media:
                history.append({"role": role, "content": text_content})
            else:
                # Handle multimodal content in history
                content_parts = [{"type": "text", "text": text_content}]
                try:
                    file_path_str = await msg.download_media(file=temp_dir)
                    if file_path_str:
                        file_path = Path(file_path_str)
                        mime_type, _ = mimetypes.guess_type(file_path)
                        if mime_type and mime_type.startswith("image/"):
                            with open(file_path, "rb") as f:
                                b64_content = base64.b64encode(f.read()).decode("utf-8")
                            content_parts.append({
                                "type": "image_url",
                                "image_url": {"url": f"data:{mime_type};base64,{b64_content}"}
                            })
                    history.append({"role": role, "content": content_parts})
                except Exception as e:
                    print(f"Warning: Could not process media for history message {msg.id}. Error: {e}")
                    # If media fails, just append the text content
                    history.append({"role": role, "content": text_content})
    finally:
        if temp_dir.exists():
            rmtree(temp_dir, ignore_errors=True)

    return history

# --- Bot Command Setup ---

async def set_bot_menu_commands():
    """Sets the bot's command menu in Telegram's UI."""
    print("LLM_Chat: setting bot commands ...")
    try:
        await asyncio.sleep(5)  # Delay to ensure client is ready
        await borg(
            SetBotCommandsRequest(
                scope=BotCommandScopeDefault(),
                lang_code="en",
                commands=[
                    BotCommand("start", "Onboard and set API key"),
                    BotCommand("help", "Show detailed help and instructions"),
                    BotCommand("setgeminikey", "Set or update your Gemini API key"),
                    BotCommand("setmodel", "Set your preferred chat model"),
                    BotCommand("setsystemprompt", "Customize the bot's instructions"),
                ],
            )
        )
        print("LLM_Chat: Bot command menu has been updated.")
    except Exception as e:
        print(f"LLM_Chat: Failed to set bot commands: {e}")


# --- Telethon Event Handlers ---

@borg.on(events.NewMessage(pattern="/start", func=lambda e: e.is_private))
async def start_handler(event):
    """Handles the /start command to onboard new users."""
    user_id = event.sender_id
    if llm_db.is_awaiting_key(user_id):
        llm_db.cancel_key_flow(user_id)
    if llm_db.get_api_key(user_id=user_id, service="gemini"):
        await event.reply(
            "Welcome back! Your Gemini API key is configured. You can start chatting with me.\n\nUse /help to see all available commands."
        )
    else:
        await llm_db.request_api_key_message(event)


@borg.on(events.NewMessage(pattern="/help", func=lambda e: e.is_private))
async def help_handler(event):
    """Provides detailed help information about features and usage."""
    if llm_db.is_awaiting_key(event.sender_id):
        llm_db.cancel_key_flow(event.sender_id)
        await event.reply("API key setup cancelled.")

    prefs = user_manager.get_prefs(event.sender_id)
    help_text = f"""
**Hello! I am a chat assistant powered by Google's Gemini.**

To get started, you'll need a free Gemini API key.
1.  **Get Your Key:** Go to **[Google AI Studio](https://aistudio.google.com/app/apikey)** to create one.
2.  **Set Your Key:** Send me the command: `/setgeminikey YOUR_API_KEY_HERE`

---

### How to Chat with Me

**▶️ Understanding Conversations (Reply Chains)**
I remember our conversations by following the **reply chain**. This is the key to having a continuous, context-aware chat.

- **Continuing the Chat:** To continue our conversation, simply **reply** to my last message.
- **Adding More Detail:** You can also **reply to your OWN message** to add more thoughts, context, or files before I've even answered. I will see it all as part of the same turn.
- **Starting Fresh:** To start a new, separate conversation, just send a new message without replying to anything.

**▶️ Advanced Features**

- **Working with Images:** Attach an image to any of your messages (in the initial message or in a reply), and I'll be able to see and discuss it.
- **Discussing Forwarded Content:** Forward messages to our chat. Then, **reply** to your forwarded message(s) with your question or prompt, and I will analyze their content.

---

### Available Commands

- `/start`: Onboard and set up your API key.
- `/help`: Shows this detailed help message.
- `/setgeminikey [API_KEY]`: Sets or updates your Gemini API key.
- `/setModel [model_id]`: Change the AI model. Your current model is: `{prefs.model}`.
- `/setSystemPrompt [prompt]`: Change my core instructions. Use `/setSystemPrompt reset` to go back to the default.
"""
    await event.reply(help_text, link_preview=False, parse_mode="md")


@borg.on(events.NewMessage(pattern=r"(?i)/setGeminiKey(?:\s+(.*))?", func=lambda e: e.is_private))
async def set_key_handler(event):
    """Delegates /setgeminikey command logic to the shared module."""
    await llm_db.handle_set_key_command(event)


@borg.on(
    events.NewMessage(
        func=lambda e: e.is_private
        and llm_db.is_awaiting_key(e.sender_id)
        and e.text and not e.text.startswith("/")
    )
)
async def key_submission_handler(event):
    """Delegates plain-text key submission logic to the shared module."""
    await llm_db.handle_key_submission(
        event,
        success_msg="You can now start chatting with me."
    )


@borg.on(events.NewMessage(pattern=r"/setModel(?:\s+(.*))?", func=lambda e: e.is_private))
async def set_model_handler(event):
    """Sets the user's preferred chat model."""
    user_id = event.sender_id
    model_name_match = event.pattern_match.group(1)
    if model_name_match:
        model_name = model_name_match.strip()
        user_manager.set_model(user_id, model_name)
        await event.reply(f"Your chat model has been set to: `{model_name}`")
    else:
        current_prefs = user_manager.get_prefs(user_id)
        await event.reply(
            f"Your current chat model is: `{current_prefs.model}`.\n\n"
            "To change it, use `/setModel <model_id>`."
        )

@borg.on(events.NewMessage(pattern=r"/setSystemPrompt(?:\s+([\s\S]+))?", func=lambda e: e.is_private))
async def set_system_prompt_handler(event):
    """Sets the user's custom system prompt or resets it to default."""
    user_id = event.sender_id
    prompt_match = event.pattern_match.group(1)
    if prompt_match:
        prompt = prompt_match.strip()
        if prompt.lower() == "reset":
            # Set the prompt to an empty string to signify using the default
            user_manager.set_system_prompt(user_id, "")
            await event.reply("Your system prompt has been reset to the default.")
        else:
            user_manager.set_system_prompt(user_id, prompt)
            await event.reply("Your new system prompt has been saved.")
    else:
        current_prefs = user_manager.get_prefs(user_id)
        prompt_to_display = current_prefs.system_prompt or "Default (no custom prompt set)"
        await event.reply(
            "**Your current system prompt is:**\n\n"
            f"```\n{prompt_to_display}\n```\n\n"
            "To change it, use `/setSystemPrompt <your new prompt>` or `/setSystemPrompt reset`."
        )

@borg.on(events.NewMessage(
    func=lambda e: e.is_private and (e.text or e.media) and not (e.text and e.text.startswith('/')) and not e.forward,
))
async def chat_handler(event):
    """Main handler for all non-command messages in a private chat."""
    user_id = event.sender_id

    # If the user is in the middle of key setup, cancel it to chat instead.
    if llm_db.is_awaiting_key(user_id):
        llm_db.cancel_key_flow(user_id)
        await event.reply("API key setup cancelled. Responding to your message instead...")

    api_key = llm_db.get_api_key(user_id=user_id, service="gemini")
    if not api_key:
        await llm_db.request_api_key_message(event)
        return

    prefs = user_manager.get_prefs(user_id)
    # This message will be edited with the streaming response
    response_message = await event.reply("...")
    temp_dir = Path(f"./temp_llm_chat_{event.id}/")

    try:
        messages = await build_conversation_history(event)

        # Add system prompt as the first message. Fallback to default if custom is empty.
        system_prompt_to_use = prefs.system_prompt or DEFAULT_SYSTEM_PROMPT
        messages.insert(0, {"role": "system", "content": system_prompt_to_use})

        # Prepare and add the current user message
        current_user_content = [{"type": "text", "text": event.message.text or ""}]
        if event.message.media:
            temp_dir.mkdir(exist_ok=True)
            try:
                file_path_str = await event.message.download_media(file=temp_dir)
                if file_path_str:
                    file_path = Path(file_path_str)
                    mime_type, _ = mimetypes.guess_type(file_path)
                    if mime_type and mime_type.startswith("image/"):
                        with open(file_path, "rb") as f:
                            b64_content = base64.b64encode(f.read()).decode("utf-8")
                        current_user_content.append({
                            "type": "image_url",
                            "image_url": {"url": f"data:{mime_type};base64,{b64_content}"}
                        })
            except Exception as e:
                 print(f"Failed to process media for current message: {e}")

        messages.append({"role": "user", "content": current_user_content})

        # Make the API call using litellm
        response_text = ""
        last_edit_time = asyncio.get_event_loop().time()
        edit_interval = 0.8  # Seconds between edits to avoid rate limits

        response_stream = await litellm.acompletion(
            model=prefs.model,
            messages=messages,
            api_key=api_key,
            stream=True
        )

        async for chunk in response_stream:
            delta = chunk.choices[0].delta.content
            if delta:
                response_text += delta
                current_time = asyncio.get_event_loop().time()
                if (current_time - last_edit_time) > edit_interval:
                    try:
                        # Add a cursor to indicate the bot is still "typing"
                        await util.edit_message(response_message, f"{response_text}▌", parse_mode="md")
                        last_edit_time = current_time
                    except errors.rpcerrorlist.MessageNotModifiedError:
                        # This error is expected if the content hasn't changed
                        pass
                    except Exception as e:
                        # Log other edit errors but don't stop the stream
                        print(f"Error during message edit: {e}")

        # Final edit to remove the cursor and show the complete message
        final_text = response_text.strip() or "__[No response]__"
        await util.edit_message(response_message, final_text, parse_mode="md", link_preview=False)

        # Log the successful conversation
        await _log_conversation(event, prefs.model, messages, final_text)

    except Exception:
        # If a major error occurs, edit the message to inform the user
        error_text = "An error occurred. You can send the inputs that caused this error to the bot developer."
        await response_message.edit(error_text)
        traceback.print_exc()
    finally:
        if temp_dir.exists():
            rmtree(temp_dir, ignore_errors=True)


# --- Initialization ---
# Schedule the command menu setup to run on the bot's event loop upon loading.
borg.loop.create_task(set_bot_menu_commands())
