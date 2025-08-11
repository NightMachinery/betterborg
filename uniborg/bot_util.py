"""
Shared utilities for implementing bot features within the Uniborg framework.
This includes command registration, generic handlers, and message processing helpers.
"""

import asyncio
from telethon import events
from telethon.tl.functions.bots import SetBotCommandsRequest
from telethon.tl.types import BotCommand, BotCommandScopeDefault

from typing import Optional, Dict
from telethon.tl.types import KeyboardButtonCallback

from uniborg import llm_db
from uniborg import util
from uniborg.constants import BOT_META_INFO_PREFIX

# --- Shared State ---

# A set to track processed media group IDs to avoid redundant processing of albums.
PROCESSED_GROUP_IDS = set()


# --- Bot Initialization ---


async def register_bot_commands(borg, commands: list[dict]):
    """
    Sets the bot's command menu in the Telegram UI.

    Args:
        borg: The Uniborg client instance.
        commands (list[dict]): A list of command dictionaries, each with
                               'command' and 'description' keys.
    """
    is_bot = await borg.is_bot()
    if not is_bot:
        print("Bot_Util: Skipping command registration for a userbot.")
        return

    print("Bot_Util: Setting bot commands...")
    try:
        # Wait a moment for the client to be fully ready
        await asyncio.sleep(5)
        await borg(
            SetBotCommandsRequest(
                scope=BotCommandScopeDefault(),
                lang_code="en",
                commands=[BotCommand(c["command"], c["description"]) for c in commands],
            )
        )
        print("Bot_Util: Bot command menu has been successfully updated.")
    except Exception as e:
        print(f"Bot_Util: Failed to set bot commands: {e}")


# --- Message Processing Utilities ---


async def expand_and_sort_messages_with_groups(event, initial_messages):
    """
    Expands a list of messages to include all members of any media groups
    represented in the initial list, returning a sorted, unique list.
    """
    final_messages_map = {m.id: m for m in initial_messages}
    processed_group_ids = set()
    messages_to_check = list(initial_messages)

    for msg in messages_to_check:
        group_id = msg.grouped_id
        if group_id and group_id not in processed_group_ids:
            try:
                # Fetch messages in the vicinity to find all group members
                k = 20  # Search range
                search_ids = range(msg.id - k, msg.id + k + 1)
                messages_in_vicinity = await event.client.get_messages(
                    event.chat_id, ids=list(search_ids)
                )
                for m in messages_in_vicinity:
                    if m and m.grouped_id == group_id:
                        final_messages_map[m.id] = m
                processed_group_ids.add(group_id)
            except Exception as e:
                print(f"Bot_Util: Could not expand message group {group_id}: {e}")

    return sorted(final_messages_map.values(), key=lambda m: m.id)


# --- Shared Model Key Sanitization ---

SANITIZATION_MAP = {
    ":": "__COLON__",
    "-": "__DASH__",
    ".": "__DOT__",
}


def sanitize_callback_data(key: str) -> str:
    """Sanitize key for use in Telegram callback data."""
    for char, replacement in SANITIZATION_MAP.items():
        key = key.replace(char, replacement)
    return key


def unsanitize_callback_data(sanitized_key: str) -> str:
    """Restore original key from sanitized callback data."""
    for char, replacement in SANITIZATION_MAP.items():
        sanitized_key = sanitized_key.replace(replacement, char)
    return sanitized_key


# --- Shared UI Components ---


async def present_options(
    event,
    *,
    title: str,
    options: Dict[str, str],
    current_value: str,
    callback_prefix: str,
    awaiting_key: str,
    n_cols: int = 2,
    awaiting_users_dict: Dict = None,
    is_bot: bool = None,
):
    """
    Present options to user as buttons (bot) or text menu (userbot).

    Args:
        event: Telethon event object
        title: Menu title
        options: Dict of {value: display_name}
        current_value: Currently selected value
        callback_prefix: Prefix for callback data
        awaiting_key: Key for userbot input tracking
        n_cols: Number of columns for buttons
        awaiting_users_dict: Dict to track pending inputs (required for userbot)
        is_bot: Whether running as bot (if None, will be determined)
    """
    user_id = event.sender_id

    # Determine if we're running as a bot
    if is_bot is None:
        is_bot = await event.client.is_bot()

    if is_bot:
        # Bot mode: show buttons
        buttons = [
            KeyboardButtonCallback(
                f"✅ {display_name}" if key == current_value else display_name,
                data=f"{callback_prefix}{sanitize_callback_data(key)}",
            )
            for key, display_name in options.items()
        ]

        title_bold = title if title.startswith("**") else f"**{title}**"
        await event.reply(
            f"{BOT_META_INFO_PREFIX}{title_bold}",
            buttons=util.build_menu(buttons, n_cols=n_cols),
            parse_mode="md",
        )
    else:
        # Userbot mode: show text menu
        if awaiting_users_dict is None:
            raise ValueError("awaiting_users_dict is required for userbot mode")

        option_keys = list(options.keys())
        menu_text = [f"**{title}**\n"]
        for i, key in enumerate(option_keys):
            display_name = options[key]
            prefix = "✅ " if key == current_value else ""
            menu_text.append(f"{i + 1}. {prefix}{display_name}")
        menu_text.append("\nPlease reply with the number of your choice.")
        menu_text.append("(Type `cancel` to stop.)")

        awaiting_users_dict[user_id] = {
            "type": awaiting_key,
            "keys": option_keys,
        }
        await event.reply(f"{BOT_META_INFO_PREFIX}\n".join(menu_text), parse_mode="md")
