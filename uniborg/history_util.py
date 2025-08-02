# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.

from collections import defaultdict, deque
from telethon import events
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
    Creates an event handler to automatically capture all incoming and outgoing
    messages and add them to the history cache.
    This should be called once when the bot starts.
    """
    if not borg:
        print("HistoryUtil Error: borg client is not set. Cannot initialize.")
        return

    @borg.on(events.NewMessage())
    async def universal_message_recorder(event: events.NewMessage.Event):
        """Records every message ID to the history cache."""
        add_message(event.chat_id, event.id)

    print("HistoryUtil: Universal message recorder has been activated.")
