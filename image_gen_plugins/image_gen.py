import asyncio
import os
import re
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Optional, Dict

import google.genai as genai
from telethon import events
from telethon.tl.types import (
    KeyboardButtonCallback,
    Message,
)
from pydantic import BaseModel, Field

# Import uniborg utilities
from uniborg import util
from uniborg import llm_db
from uniborg import bot_util
from uniborg.storage import UserStorage
from uniborg.constants import BOT_META_INFO_PREFIX

# --- Constants and Configuration ---
DEFAULT_MODEL = "imagen-3.0-generate-001"

# Available models for image generation
MODEL_CHOICES = {
    "imagen-3.0-generate-001": "Imagen 3.0 Generate",
    "imagen-3.0-fast-generate-001": "Imagen 3.0 Fast Generate",
}

# Image size options
SIZE_CHOICES = {
    "512x512": "512×512 (Small)",
    "1024x1024": "1024×1024 (Standard)",
    "1536x1536": "1536×1536 (Large)",
}

# Aspect ratio options (for models that support it)
ASPECT_RATIO_CHOICES = {
    "1:1": "Square (1:1)",
    "4:3": "Landscape (4:3)", 
    "16:9": "Widescreen (16:9)",
    "9:16": "Vertical (9:16)",
    "3:4": "Portrait (3:4)",
}

# Quality options
QUALITY_CHOICES = {
    "standard": "Standard Quality",
    "high": "High Quality",
}

# Number of images options
NUMBER_CHOICES = {
    "1": "1 image",
    "2": "2 images", 
    "3": "3 images",
    "4": "4 images",
}

# Bot commands
BOT_COMMANDS = [
    {"command": "start", "description": "Set up image generation bot"},
    {"command": "help", "description": "Show detailed help"},
    {"command": "status", "description": "Show current image generation settings"},
    {"command": "setgeminikey", "description": "Set Gemini API key for image generation"},
    {"command": "model", "description": "Select image generation model"},
    {"command": "size", "description": "Set image size"},
    {"command": "aspectratio", "description": "Set image aspect ratio"},
    {"command": "quality", "description": "Set image quality"},
    {"command": "number", "description": "Set number of images to generate"},
    {"command": "enhanceprompt", "description": "Toggle prompt enhancement"},
]

# Create command set for lookup
KNOWN_COMMAND_SET = {f"/{cmd['command']}".lower() for cmd in BOT_COMMANDS}

# State management
BOT_USERNAME = None
BOT_ID = None
IS_BOT = None
AWAITING_INPUT_FROM_USERS = {}

# --- User Preferences Management ---
class ImageGenPrefs(BaseModel):
    """Pydantic model for image generation preferences."""
    
    model: str = Field(default=DEFAULT_MODEL)
    size: str = Field(default="1024x1024")
    aspect_ratio: str = Field(default="1:1")
    quality: str = Field(default="standard")
    number: int = Field(default=1)
    enhance_prompt: bool = Field(default=False)
    person_generation: str = Field(default="allow")
    safety_filter_level: str = Field(default="block_only_high")
    add_watermark: bool = Field(default=False)

class UserManager:
    """Manager for user preferences."""
    
    def __init__(self):
        self.storage = UserStorage(purpose="image_gen")
    
    def get_prefs(self, user_id: int) -> ImageGenPrefs:
        data = self.storage.get(user_id)
        return ImageGenPrefs.model_validate(data or {})
    
    def _save_prefs(self, user_id: int, prefs: ImageGenPrefs):
        self.storage.set(user_id, prefs.model_dump(exclude_defaults=True))
    
    def set_model(self, user_id: int, model: str):
        prefs = self.get_prefs(user_id)
        prefs.model = model
        self._save_prefs(user_id, prefs)
    
    def set_size(self, user_id: int, size: str):
        prefs = self.get_prefs(user_id)
        prefs.size = size
        self._save_prefs(user_id, prefs)
    
    def set_aspect_ratio(self, user_id: int, ratio: str):
        prefs = self.get_prefs(user_id)
        prefs.aspect_ratio = ratio
        self._save_prefs(user_id, prefs)
    
    def set_quality(self, user_id: int, quality: str):
        prefs = self.get_prefs(user_id)
        prefs.quality = quality
        self._save_prefs(user_id, prefs)
    
    def set_number(self, user_id: int, number: int):
        prefs = self.get_prefs(user_id)
        prefs.number = number
        self._save_prefs(user_id, prefs)
    
    def toggle_enhance_prompt(self, user_id: int) -> bool:
        prefs = self.get_prefs(user_id)
        prefs.enhance_prompt = not prefs.enhance_prompt
        self._save_prefs(user_id, prefs)
        return prefs.enhance_prompt

# Initialize managers
user_manager = UserManager()

def cancel_input_flow(user_id: int):
    """Cancels any pending input requests for a user."""
    AWAITING_INPUT_FROM_USERS.pop(user_id, None)

# --- Command Handlers ---
async def start_handler(event):
    """Handle /start command."""
    user_id = event.sender_id
    cancel_input_flow(user_id)
    
    api_key = llm_db.get_api_key(user_id)
    if not api_key:
        await event.reply(
            f"{BOT_META_INFO_PREFIX}**Welcome to Image Generation Bot! 🎨**\n\n"
            "To get started, you need to set your Gemini API key.\n"
            "Use `/setgeminikey` to configure your API key.\n\n"
            "Once configured, simply send me any message and I'll generate images for you!"
        )
    else:
        await event.reply(
            f"{BOT_META_INFO_PREFIX}**Image Generation Bot is ready! 🎨**\n\n"
            "Send me any text message and I'll generate images based on your prompt.\n\n"
            "Use `/help` to see available commands for customizing image generation."
        )

async def help_handler(event):
    """Handle /help command."""
    help_text = f"""**Image Generation Bot Help 🎨**

**How it works:**
• Send any text message → Get AI-generated images
• Only works in private chats
• Forwarded messages are ignored

**Configuration Commands:**
• `/status` - View current settings
• `/model` - Select AI model
• `/size` - Set image dimensions
• `/aspectratio` - Choose aspect ratio
• `/quality` - Set image quality
• `/number` - Number of images (1-4)
• `/enhanceprompt` - Toggle prompt enhancement

**Setup:**
• `/setgeminikey` - Configure API access

**Current Model:** Imagen 3.0
**Safety Settings:** Minimal censoring enabled
"""
    await event.reply(f"{BOT_META_INFO_PREFIX}{help_text}")

async def status_handler(event):
    """Handle /status command."""
    user_id = event.sender_id
    prefs = user_manager.get_prefs(user_id)
    api_key = llm_db.get_api_key(user_id)
    
    status_text = f"""**Image Generation Settings 🎨**

**API Status:** {"✅ Configured" if api_key else "❌ Not Set"}
**Model:** {MODEL_CHOICES.get(prefs.model, prefs.model)}
**Size:** {prefs.size}
**Aspect Ratio:** {prefs.aspect_ratio}
**Quality:** {prefs.quality.title()}
**Number of Images:** {prefs.number}
**Enhance Prompt:** {"✅ Enabled" if prefs.enhance_prompt else "❌ Disabled"}

**Safety Settings:**
**Person Generation:** {prefs.person_generation.title()}
**Safety Filter:** {prefs.safety_filter_level.replace('_', ' ').title()}
**Watermark:** {"Disabled" if not prefs.add_watermark else "Enabled"}
"""
    await event.reply(f"{BOT_META_INFO_PREFIX}{status_text}")

async def model_handler(event):
    """Handle /model command."""
    user_id = event.sender_id
    prefs = user_manager.get_prefs(user_id)
    
    await bot_util.present_options(
        event,
        title="Select Image Generation Model",
        options=MODEL_CHOICES,
        current_value=prefs.model,
        callback_prefix="model_",
        awaiting_key="model_selection",
        n_cols=1,
        awaiting_users_dict=AWAITING_INPUT_FROM_USERS,
        is_bot=IS_BOT,
    )

async def size_handler(event):
    """Handle /size command."""
    user_id = event.sender_id
    prefs = user_manager.get_prefs(user_id)
    
    await bot_util.present_options(
        event,
        title="Select Image Size",
        options=SIZE_CHOICES,
        current_value=prefs.size,
        callback_prefix="size_",
        awaiting_key="size_selection",
        n_cols=1,
        awaiting_users_dict=AWAITING_INPUT_FROM_USERS,
        is_bot=IS_BOT,
    )

async def aspect_ratio_handler(event):
    """Handle /aspectratio command."""
    user_id = event.sender_id
    prefs = user_manager.get_prefs(user_id)
    
    await bot_util.present_options(
        event,
        title="Select Aspect Ratio",
        options=ASPECT_RATIO_CHOICES,
        current_value=prefs.aspect_ratio,
        callback_prefix="ratio_",
        awaiting_key="ratio_selection",
        n_cols=1,
        awaiting_users_dict=AWAITING_INPUT_FROM_USERS,
        is_bot=IS_BOT,
    )

async def quality_handler(event):
    """Handle /quality command."""
    user_id = event.sender_id
    prefs = user_manager.get_prefs(user_id)
    
    await bot_util.present_options(
        event,
        title="Select Image Quality",
        options=QUALITY_CHOICES,
        current_value=prefs.quality,
        callback_prefix="quality_",
        awaiting_key="quality_selection",
        n_cols=2,
        awaiting_users_dict=AWAITING_INPUT_FROM_USERS,
        is_bot=IS_BOT,
    )

async def number_handler(event):
    """Handle /number command."""
    user_id = event.sender_id
    prefs = user_manager.get_prefs(user_id)
    
    await bot_util.present_options(
        event,
        title="Number of Images to Generate",
        options=NUMBER_CHOICES,
        current_value=str(prefs.number),
        callback_prefix="number_",
        awaiting_key="number_selection",
        n_cols=2,
        awaiting_users_dict=AWAITING_INPUT_FROM_USERS,
        is_bot=IS_BOT,
    )

async def enhance_prompt_handler(event):
    """Handle /enhanceprompt command."""
    user_id = event.sender_id
    is_enabled = user_manager.toggle_enhance_prompt(user_id)
    
    status = "enabled" if is_enabled else "disabled"
    await event.reply(f"{BOT_META_INFO_PREFIX}Prompt enhancement {status}.")

async def set_gemini_key_handler(event):
    """Handle /setgeminikey command."""
    user_id = event.sender_id
    await event.reply(
        f"{BOT_META_INFO_PREFIX}Please use the main bot's `/setgeminikey` command to configure your API key.\n\n"
        "The image generation bot uses the same Gemini API key as the main LLM chat bot."
    )

# --- Callback Handler ---
async def callback_handler(event):
    """Handle inline button presses."""
    data_str = event.data.decode("utf-8")
    user_id = event.sender_id
    prefs = user_manager.get_prefs(user_id)
    
    if data_str.startswith("model_"):
        model_id = bot_util.unsanitize_callback_data(data_str.split("_", 1)[1])
        user_manager.set_model(user_id, model_id)
        cancel_input_flow(user_id)
        
        # Update button display
        prefs = user_manager.get_prefs(user_id)
        buttons = [
            KeyboardButtonCallback(
                f"✅ {name}" if key == prefs.model else name,
                data=f"model_{bot_util.sanitize_callback_data(key)}",
            )
            for key, name in MODEL_CHOICES.items()
        ]
        await event.edit(buttons=util.build_menu(buttons, n_cols=1))
        await event.answer(f"Model set to {MODEL_CHOICES[model_id]}")
    
    elif data_str.startswith("size_"):
        size = bot_util.unsanitize_callback_data(data_str.split("_", 1)[1])
        user_manager.set_size(user_id, size)
        
        prefs = user_manager.get_prefs(user_id)
        buttons = [
            KeyboardButtonCallback(
                f"✅ {name}" if key == prefs.size else name,
                data=f"size_{bot_util.sanitize_callback_data(key)}",
            )
            for key, name in SIZE_CHOICES.items()
        ]
        await event.edit(buttons=util.build_menu(buttons, n_cols=1))
        await event.answer(f"Size set to {size}")
    
    elif data_str.startswith("ratio_"):
        ratio = bot_util.unsanitize_callback_data(data_str.split("_", 1)[1])
        user_manager.set_aspect_ratio(user_id, ratio)
        
        prefs = user_manager.get_prefs(user_id)
        buttons = [
            KeyboardButtonCallback(
                f"✅ {name}" if key == prefs.aspect_ratio else name,
                data=f"ratio_{bot_util.sanitize_callback_data(key)}",
            )
            for key, name in ASPECT_RATIO_CHOICES.items()
        ]
        await event.edit(buttons=util.build_menu(buttons, n_cols=1))
        await event.answer(f"Aspect ratio set to {ratio}")
    
    elif data_str.startswith("quality_"):
        quality = data_str.split("_", 1)[1]
        user_manager.set_quality(user_id, quality)
        
        prefs = user_manager.get_prefs(user_id)
        buttons = [
            KeyboardButtonCallback(
                f"✅ {name}" if key == prefs.quality else name,
                data=f"quality_{key}",
            )
            for key, name in QUALITY_CHOICES.items()
        ]
        await event.edit(buttons=util.build_menu(buttons, n_cols=2))
        await event.answer(f"Quality set to {quality}")
    
    elif data_str.startswith("number_"):
        number = int(data_str.split("_", 1)[1])
        user_manager.set_number(user_id, number)
        
        prefs = user_manager.get_prefs(user_id)
        buttons = [
            KeyboardButtonCallback(
                f"✅ {name}" if key == str(prefs.number) else name,
                data=f"number_{key}",
            )
            for key, name in NUMBER_CHOICES.items()
        ]
        await event.edit(buttons=util.build_menu(buttons, n_cols=2))
        await event.answer(f"Number of images set to {number}")

# --- Image Generation ---
async def generate_image(prompt: str, user_id: int) -> list:
    """Generate images using Google Gen AI API."""
    api_key = llm_db.get_api_key(user_id)
    if not api_key:
        raise ValueError("No API key configured")
    
    prefs = user_manager.get_prefs(user_id)
    
    try:
        # Initialize client
        client = genai.Client(api_key=api_key)
        
        # Prepare configuration
        config = genai.types.GenerateImagesConfig(
            number_of_images=prefs.number,
            include_rai_reason=True,
            output_mime_type='image/jpeg',
            # Add safety settings for minimal censoring
            person_generation=prefs.person_generation,
            add_watermark=prefs.add_watermark,
        )
        
        # Add aspect ratio if specified
        if prefs.aspect_ratio != "1:1":
            config.aspect_ratio = prefs.aspect_ratio
        
        # Generate images
        response = client.models.generate_images(
            model=prefs.model,
            prompt=prompt,
            config=config,
        )
        
        return response.generated_images
    except Exception as e:
        raise Exception(f"Image generation failed: {str(e)}")

# --- Message Handler ---
async def message_handler(event):
    """Handle incoming messages for image generation."""
    # Only process private, non-forwarded messages that aren't commands
    if not event.is_private or event.forward or event.text.lower().startswith('/'):
        return
    
    user_id = event.sender_id
    api_key = llm_db.get_api_key(user_id)
    
    if not api_key:
        await event.reply(
            f"{BOT_META_INFO_PREFIX}❌ **No API key configured**\n\n"
            "Use `/setgeminikey` to set up your Gemini API key first."
        )
        return
    
    # Skip if user is in input flow
    if user_id in AWAITING_INPUT_FROM_USERS:
        return
    
    prompt = event.text.strip()
    if not prompt:
        return
    
    # Send "generating" message
    status_msg = await event.reply(f"{BOT_META_INFO_PREFIX}🎨 Generating images...")
    
    try:
        # Generate images
        images = await generate_image(prompt, user_id)
        
        # Send images
        if images:
            await status_msg.edit(f"{BOT_META_INFO_PREFIX}✅ Generated {len(images)} image(s):")
            
            for i, generated_image in enumerate(images, 1):
                # Save image temporarily and send
                with tempfile.NamedTemporaryFile(suffix='.jpg', delete=False) as tmp_file:
                    # Get image data and save
                    image_data = generated_image.image._pil_image
                    image_data.save(tmp_file.name, format='JPEG')
                    
                    caption = f"Image {i}/{len(images)}\nPrompt: {prompt[:100]}..."
                    await event.reply(file=tmp_file.name, caption=caption)
                    os.unlink(tmp_file.name)  # Clean up
        else:
            await status_msg.edit(f"{BOT_META_INFO_PREFIX}❌ No images were generated.")
            
    except Exception as e:
        await status_msg.edit(f"{BOT_META_INFO_PREFIX}❌ Error: {str(e)}")

# --- Handler Registration ---
def register_handlers():
    """Register all event handlers."""
    bot_username_suffix_re = f"(?:{re.escape(BOT_USERNAME)})?" if BOT_USERNAME else ""
    
    # Command handlers
    borg.on(events.NewMessage(
        pattern=rf"(?i)^/start{bot_username_suffix_re}\s*$",
        func=lambda e: e.is_private,
    ))(start_handler)
    
    borg.on(events.NewMessage(
        pattern=rf"(?i)^/help{bot_username_suffix_re}\s*$",
        func=lambda e: e.is_private,
    ))(help_handler)
    
    borg.on(events.NewMessage(
        pattern=rf"(?i)^/status{bot_username_suffix_re}\s*$",
        func=lambda e: e.is_private,
    ))(status_handler)
    
    borg.on(events.NewMessage(
        pattern=rf"(?i)^/model{bot_username_suffix_re}\s*$",
        func=lambda e: e.is_private,
    ))(model_handler)
    
    borg.on(events.NewMessage(
        pattern=rf"(?i)^/size{bot_username_suffix_re}\s*$",
        func=lambda e: e.is_private,
    ))(size_handler)
    
    borg.on(events.NewMessage(
        pattern=rf"(?i)^/aspectratio{bot_username_suffix_re}\s*$",
        func=lambda e: e.is_private,
    ))(aspect_ratio_handler)
    
    borg.on(events.NewMessage(
        pattern=rf"(?i)^/quality{bot_username_suffix_re}\s*$",
        func=lambda e: e.is_private,
    ))(quality_handler)
    
    borg.on(events.NewMessage(
        pattern=rf"(?i)^/number{bot_username_suffix_re}\s*$",
        func=lambda e: e.is_private,
    ))(number_handler)
    
    borg.on(events.NewMessage(
        pattern=rf"(?i)^/enhanceprompt{bot_username_suffix_re}\s*$",
        func=lambda e: e.is_private,
    ))(enhance_prompt_handler)
    
    borg.on(events.NewMessage(
        pattern=rf"(?i)^/setgeminikey{bot_username_suffix_re}\s*$",
        func=lambda e: e.is_private,
    ))(set_gemini_key_handler)
    
    # Message handler for image generation
    borg.on(events.NewMessage(func=lambda e: e.is_private and not e.forward))(message_handler)
    
    # Callback handler
    borg.on(events.CallbackQuery())(callback_handler)
    
    print("ImageGen: All event handlers registered.")

# --- Initialization ---
async def initialize_image_gen():
    """Initialize the image generation bot."""
    global BOT_ID, BOT_USERNAME, IS_BOT
    
    if IS_BOT is None:
        IS_BOT = await borg.is_bot()
    
    if BOT_USERNAME is None:
        me = await borg.get_me()
        BOT_ID = me.id
        if me.username:
            BOT_USERNAME = f"@{me.username}"
        else:
            if not IS_BOT:
                print("ImageGen: Running as USERBOT.")
    
    # Register handlers
    register_handlers()
    
    # Set bot commands if running as bot
    await bot_util.register_bot_commands(borg, BOT_COMMANDS)

# Schedule initialization
borg.loop.create_task(initialize_image_gen())
