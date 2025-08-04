# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.

from collections import defaultdict, deque
from datetime import datetime
from telethon import events
from telethon.tl.types import Message
from typing import List, Deque, DefaultDict, Tuple

# --- Client Instance & Constants ---
# The borg client instance will be populated by `_async_init` in `uniborg/uniborg.py`.
borg = None
HISTORY_LIMIT = 2000  # Max number of message IDs to store per chat.

# In-memory store for message IDs and their timestamps
# {chat_id: deque([(msg_id, timestamp), ...])}
_history_cache: DefaultDict[int, Deque[Tuple[int, datetime]]] = defaultdict(
    lambda: deque(maxlen=HISTORY_LIMIT)
)


# --- Public API ---


def add_message(chat_id: int, message_id: int, timestamp: datetime):
    """Adds a message ID and its timestamp to the in-memory history."""
    _history_cache[chat_id].append((message_id, timestamp))


def get_last_n_ids(chat_id: int, n: int) -> List[int]:
    """
    Retrieves the last N message IDs for a given chat from the cache.
    Returns them in chronological order (oldest to newest).
    """
    chat_history = _history_cache.get(chat_id, deque())
    # Return only the message IDs
    return [item[0] for item in list(chat_history)[-n:]]


def get_all_ids(chat_id: int) -> List[int]:
    """
    Retrieves all cached message IDs for a given chat in chronological order.
    """
    # Return only the message IDs
    return [item[0] for item in _history_cache.get(chat_id, deque())]


def get_ids_since(chat_id: int, timestamp: datetime) -> List[int]:
    """
    Retrieves message IDs for a chat that have occurred since the given timestamp.
    """
    chat_history = _history_cache.get(chat_id, deque())
    return [msg_id for msg_id, msg_ts in chat_history if msg_ts > timestamp]


def clear_chat_history(chat_id: int):
    """Clears the history for a specific chat."""
    if chat_id in _history_cache:
        _history_cache[chat_id].clear()


# --- Automatic History Population ---


async def initialize_history_handler():
    """
    Initializes history tracking. It uses an event handler for userbots and
    monkey-patches the send methods for official bots to ensure all outgoing
    messages are logged correctly.
    This should be called once when the bot starts.
    """
    if not borg:
        print("HistoryUtil Error: borg client is not set. Cannot initialize.")
        return

    # --- 1. Handler for Incoming Messages (works for both users and bots) ---
    @borg.on(events.NewMessage(incoming=True))
    async def incoming_message_recorder(event: events.NewMessage.Event):
        """Records every incoming message ID to the history cache."""
        add_message(event.chat_id, event.id, event.date)

    # --- 2. Check if running as a bot or user and apply the correct strategy ---
    if await borg.is_bot():
        # BOT MODE: Monkey-patch send methods, as bots don't get outgoing events.

        # Add a guard to prevent patching the client more than once
        if hasattr(borg, "_history_patched"):
            return
        borg._history_patched = True

        # Store the original methods before we replace them
        original_send_message = borg.send_message
        original_send_file = borg.send_file

        async def patched_send_message(*args, **kwargs):
            # Call the original function to actually send the message
            sent_message: Message = await original_send_message(*args, **kwargs)
            # After the message is sent, log its ID
            if sent_message:
                add_message(sent_message.chat_id, sent_message.id, sent_message.date)
            return sent_message

        async def patched_send_file(*args, **kwargs):
            # Call the original function
            result = await original_send_file(*args, **kwargs)
            # send_file can return a single Message or a list of Messages (for albums)
            if result:
                if isinstance(result, list):
                    for sent_message in result:
                        if sent_message:
                            add_message(
                                sent_message.chat_id, sent_message.id, sent_message.date
                            )
                else:
                    add_message(result.chat_id, result.id, result.date)
            return result

        # Replace the methods on the live client instance with our new versions
        borg.send_message = patched_send_message
        borg.send_file = patched_send_file

        print(
            "HistoryUtil (Bot Mode): Incoming recorder active, send methods patched for outgoing history."
        )

    else:
        # USER MODE: Use the standard event handler for outgoing messages.
        @borg.on(events.NewMessage(outgoing=True))
        async def outgoing_message_recorder(event: events.NewMessage.Event):
            """Records every outgoing message ID to the history cache."""
            add_message(event.chat_id, event.id, event.date)

        print(
            "HistoryUtil (User Mode): Incoming and outgoing message recorders have been activated."
        )
