"""
Shared utilities for implementing bot features within the Uniborg framework.
This includes command registration, generic handlers, and message processing helpers.
"""

import asyncio
from telethon import events
from telethon.tl.functions.bots import SetBotCommandsRequest
from telethon.tl.types import BotCommand, BotCommandScopeDefault

from uniborg import llm_db

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
