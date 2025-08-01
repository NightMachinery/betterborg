import asyncio
import traceback
import os
import uuid
from datetime import datetime
from pathlib import Path
from shutil import rmtree

import llm
from telethon import events
from pydantic import BaseModel, Field

# Import uniborg utilities and storage
from uniborg import util
from uniborg import llm_db
from uniborg.storage import UserStorage

# --- Constants and Configuration ---

DEFAULT_MODEL = "gemini-2.5-flash"  # 2.5 is the latest model
DEFAULT_SYSTEM_PROMPT = """
You are a helpful and knowledgeable assistant. Your primary audience is advanced STEM postgraduate researchers, so be precise and technically accurate.

**Style Guidelines for Mobile Chat:**
- **Concise & Direct:** Keep responses as brief as possible without sacrificing critical information. Get straight to the point.
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

async def _log_conversation(event, model_name: str, conversation_history: list, final_response: str):
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

        for resp in conversation_history:
            # A user turn has a prompt, an assistant turn has a response
            if resp.prompt.prompt: # User Turn
                log_parts.append("\n[User]:")
                if resp.prompt.attachments:
                    for att in resp.prompt.attachments:
                        # Log a placeholder, not the content
                        att_type = att.type or "file"
                        log_parts.append(f"[Attachment: {att_type}]")
                log_parts.append(resp.prompt.prompt)
            elif resp.response: # Assistant Turn
                log_parts.append("\n[Assistant]:")
                log_parts.append(resp.response)

        # Add the final response from the current interaction
        log_parts.append("\n[Assistant]:")
        log_parts.append(final_response)

        with open(log_file_path, "w", encoding="utf-8") as f:
            f.write("\n".join(log_parts))

    except Exception as e:
        print(f"Failed to write chat log for user {event.sender_id}: {e}")
        traceback.print_exc()


async def build_conversation_history(event) -> list[llm.Response]:
    """
    Constructs a conversation history from the reply chain, downloading attachments.
    """
    history = []
    message = event.message
    bot_me = await event.client.get_me()
    temp_dir = Path(f"./temp_llm_chat_{event.id}/")
    temp_dir.mkdir(exist_ok=True)

    try:
        fake_model = llm.get_model(DEFAULT_MODEL)
        while message:
            role = "assistant" if message.sender_id == bot_me.id else "user"
            text_content = message.text or ""
            attachments = []
            if message.media:
                try:
                    file_path = await message.download_media(file=temp_dir)
                    if file_path:
                        attachments.append(llm.Attachment(path=file_path))
                except Exception as e:
                    print(f"Warning: Could not download media for message {message.id}. Error: {e}")

            prompt_obj = llm.Prompt(text_content, model=fake_model, attachments=attachments)
            if role == "assistant":
                response_obj = llm.Response.fake(model=fake_model, prompt=llm.Prompt("", model=fake_model), system="", response=text_content)
            else:
                response_obj = llm.Response.fake(model=fake_model, prompt=prompt_obj, system="", response="")
            history.append(response_obj)

            if not message.reply_to_msg_id:
                break
            message = await event.client.get_messages(event.chat_id, ids=message.reply_to_msg_id)
    finally:
        if temp_dir.exists():
            rmtree(temp_dir, ignore_errors=True)
    history.reverse()
    return history

# --- Telethon Event Handlers ---

@borg.on(util.admin_cmd(pattern=r"/setModel(?:\s+(.*))?"))
async def set_model_handler(event):
    # (This handler is unchanged)
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

@borg.on(util.admin_cmd(pattern=r"/setSystemPrompt(?:\s+([\s\S]+))?"))
async def set_system_prompt_handler(event):
    # (This handler is unchanged)
    user_id = event.sender_id
    prompt_match = event.pattern_match.group(1)
    if prompt_match:
        prompt = prompt_match.strip()
        if prompt.lower() == "reset":
            user_manager.set_system_prompt(user_id, DEFAULT_SYSTEM_PROMPT)
            await event.reply("Your system prompt has been reset to the default.")
        else:
            user_manager.set_system_prompt(user_id, prompt)
            await event.reply("Your new system prompt has been saved.")
    else:
        current_prefs = user_manager.get_prefs(user_id)
        await event.reply(
            "**Your current system prompt is:**\n\n"
            f"```\n{current_prefs.system_prompt}\n```\n\n"
            "To change it, use `/setSystemPrompt <your new prompt>` or `/setSystemPrompt reset`."
        )

@borg.on(events.NewMessage(
    func=lambda e: e.is_private and e.text and not e.text.startswith('/') and not e.forward,
))
async def chat_handler(event):
    """Main handler for all non-command messages in a private chat."""
    user_id = event.sender_id
    api_key = llm_db.get_api_key(user_id=user_id, service="gemini")
    if not api_key:
        await llm_db.request_api_key_message(event)
        return

    prefs = user_manager.get_prefs(user_id)
    try:
        model = llm.get_async_model(prefs.model)
    except llm.UnknownModelError:
        await event.reply(f"Error: Your configured model (`{prefs.model}`) was not found. Use `/setModel` to fix it.")
        return

    thinking_message = await event.reply("...")
    conversation = None # Define conversation here to access in finally block
    try:
        conversation = model.conversation()
        history = await build_conversation_history(event)
        conversation.responses = history

        response_text = ""
        last_edit_time = asyncio.get_event_loop().time()
        prompt_text = event.message.text or ""

        async for chunk in conversation.prompt(
            prompt_text,
            system=prefs.system_prompt,
            key=api_key
        ):
            response_text += chunk
            current_time = asyncio.get_event_loop().time()
            if (current_time - last_edit_time) > 1.5 and response_text.strip():
                await thinking_message.edit(f"{response_text}▌")
                last_edit_time = current_time

        final_text = response_text.strip() or "__[No response]__"
        await util.discreet_send(
            event, final_text, reply_to=event.message, parse_mode="md",
        )
        await thinking_message.delete()

        # Log the successful conversation
        await _log_conversation(event, prefs.model, conversation.responses, final_text)

    except Exception:
        await util.handle_exc(event, reply_exc=False)
        await thinking_message.edit("An error occurred. The details have been logged to the console.")
