# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.

from collections import defaultdict, deque
from telethon import events
from telethon.tl.types import Message
from typing import List, Deque, DefaultDict

# --- Client Instance & Constants ---
# The borg client instance will be populated by `_async_init` in `uniborg/uniborg.py`.
borg = None
HISTORY_LIMIT = 2000  # Max number of message IDs to store per chat.

# In-memory store for message IDs
# {chat_id: deque([msg_id_1, msg_id_2, ...])}
_history_cache: DefaultDict[int, Deque[int]] = defaultdict(
    lambda: deque(maxlen=HISTORY_LIMIT)
)


# --- Public API ---


def add_message(chat_id: int, message_id: int):
    """Adds a message ID to the in-memory history for a given chat."""
    _history_cache[chat_id].append(message_id)


def get_last_n_ids(chat_id: int, n: int) -> List[int]:
    """
    Retrieves the last N message IDs for a given chat from the cache.
    Returns them in chronological order (oldest to newest).
    """
    chat_history = _history_cache.get(chat_id, deque())
    return list(chat_history)[-n:]


def get_all_ids(chat_id: int) -> List[int]:
    """
    Retrieves all cached message IDs for a given chat in chronological order.
    """
    return list(_history_cache.get(chat_id, deque()))


# --- Automatic History Population ---


def initialize_history_handler():
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
        add_message(event.chat_id, event.id)

    # --- 2. Check if running as a bot or user and apply the correct strategy ---
    if borg.me.bot:
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
                add_message(sent_message.chat_id, sent_message.id)
            return sent_message

        async def patched_send_file(*args, **kwargs):
            # Call the original function
            result = await original_send_file(*args, **kwargs)
            # send_file can return a single Message or a list of Messages (for albums)
            if result:
                if isinstance(result, list):
                    for sent_message in result:
                        if sent_message:
                            add_message(sent_message.chat_id, sent_message.id)
                else:
                    add_message(result.chat_id, result.id)
            return result

        # Replace the methods on the live client instance with our new versions
        borg.send_message = patched_send_message
        borg.send_file = patched_send_file

        print("HistoryUtil (Bot Mode): Incoming recorder active, send methods patched for outgoing history.")

    else:
        # USER MODE: Use the standard event handler for outgoing messages.
        @borg.on(events.NewMessage(outgoing=True))
        async def outgoing_message_recorder(event: events.NewMessage.Event):
            """Records every outgoing message ID to the history cache."""
            add_message(event.chat_id, event.id)

        print("HistoryUtil (User Mode): Incoming and outgoing message recorders have been activated.")
