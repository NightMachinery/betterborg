# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.

from collections import defaultdict, deque
from datetime import datetime
from telethon import events
from telethon.tl.types import Message
import telethon.utils
from typing import List, Deque, DefaultDict, Dict
from dataclasses import dataclass, replace

# --- Data Structures ---


@dataclass(frozen=True)
class HistoryItem:
    """Represents a single message entry in our history cache."""
    message_id: int
    timestamp: datetime
    deleted: bool = False

# A global lookup map for O(1) chat_id retrieval from a message_id.
# This is the core of the performance optimization for handling deletions.
_message_id_to_chat_id_map: Dict[int, int] = {}


class EvictionTrackingDeque(deque):
    """
    A deque subclass that automatically removes an item's corresponding
    entry from the global lookup map when that item is evicted.
    """
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

    def append(self, item: HistoryItem):
        # Before appending, check if the deque is full.
        if self.maxlen is not None and len(self) == self.maxlen:
            # If so, the leftmost item is about to be evicted.
            # We must remove it from our lookup map.
            evicted_item = self[0]
            _message_id_to_chat_id_map.pop(evicted_item.message_id, None)
        # Now, perform the actual append operation.
        super().append(item)

    def clear(self):
        # When clearing a chat's history, remove all its messages from the map.
        for item in self:
            _message_id_to_chat_id_map.pop(item.message_id, None)
        super().clear()


# --- Client Instance & Constants ---
# The borg client instance will be populated by `_async_init` in `uniborg/uniborg.py`.
borg = None
HISTORY_LIMIT = 2000  # Max number of message IDs to store per chat.

# In-memory store using our custom EvictionTrackingDeque.
# {chat_id: EvictionTrackingDeque([HistoryItem, ...])}
_history_cache: DefaultDict[int, Deque[HistoryItem]] = defaultdict(
    lambda: EvictionTrackingDeque(maxlen=HISTORY_LIMIT)
)


# --- Public API ---


def add_message(chat_id: int, message_id: int, timestamp: datetime):
    """
    Adds a new message to the history, syncing both the cache and the lookup map.
    """
    # 1. Add the message to the chat's history deque.
    #    The deque itself handles evicting old entries from the lookup map.
    _history_cache[chat_id].append(
        HistoryItem(message_id=message_id, timestamp=timestamp)
    )
    # 2. Add the new message to our lookup map for fast deletion handling.
    _message_id_to_chat_id_map[message_id] = chat_id


def mark_as_deleted(chat_id: int, message_ids: List[int]):
    """Marks a list of message IDs as deleted for a specific chat."""
    chat_history = _history_cache.get(chat_id)
    if not chat_history:
        return

    updated_history = EvictionTrackingDeque(maxlen=HISTORY_LIMIT)
    message_ids_set = set(message_ids)

    for item in chat_history:
        if item.message_id in message_ids_set:
            updated_history.append(replace(item, deleted=True))
        else:
            updated_history.append(item)

    _history_cache[chat_id] = updated_history


def get_last_n_ids(chat_id: int, n: int, skip_deleted_p: bool = True) -> List[int]:
    """
    Retrieves the last N message IDs for a given chat from the cache.
    """
    chat_history = _history_cache.get(chat_id, deque())
    if skip_deleted_p:
        filtered_ids = [item.message_id for item in chat_history if not item.deleted]
        return filtered_ids[-n:]
    else:
        return [item.message_id for item in list(chat_history)[-n:]]


def get_all_ids(chat_id: int, skip_deleted_p: bool = True) -> List[int]:
    """
    Retrieves all cached message IDs for a given chat.
    """
    chat_history = _history_cache.get(chat_id, deque())
    if skip_deleted_p:
        return [item.message_id for item in chat_history if not item.deleted]
    else:
        return [item.message_id for item in chat_history]


def get_ids_since(
    chat_id: int, timestamp: datetime, skip_deleted_p: bool = True
) -> List[int]:
    """
    Retrieves message IDs for a chat that have occurred since the given timestamp.
    """
    chat_history = _history_cache.get(chat_id, deque())
    if skip_deleted_p:
        return [
            item.message_id
            for item in chat_history
            if item.timestamp > timestamp and not item.deleted
        ]
    else:
        return [item.message_id for item in chat_history if item.timestamp > timestamp]


def clear_chat_history(chat_id: int):
    """Clears the history for a specific chat."""
    if chat_id in _history_cache:
        # Our custom deque's clear() method handles map cleanup.
        _history_cache[chat_id].clear()


# --- Automatic History Population ---


async def initialize_history_handler():
    """
    Initializes history tracking. It uses event handlers and monkey-patching
    to log new, outgoing, and deleted messages.
    """
    global borg
    if not borg:
        print("HistoryUtil Error: borg client is not set. Cannot initialize.")
        return

    # --- 1. Handler for Incoming Messages ---
    @borg.on(events.NewMessage(incoming=True))
    async def incoming_message_recorder(event: events.NewMessage.Event):
        add_message(event.chat_id, event.id, event.date)

    # --- 2. Handler for Deleted Messages (Now highly efficient) ---
    @borg.on(events.MessageDeleted)
    async def message_deleted_recorder(event: events.MessageDeleted.Event):
        if not event.deleted_ids:
            return

        # Group deleted IDs by the chat they belong to.
        deletions_by_chat: DefaultDict[int, List[int]] = defaultdict(list)
        for msg_id in event.deleted_ids:
            # Instantly find the chat_id using our lookup map.
            chat_id = _message_id_to_chat_id_map.get(msg_id)
            if chat_id:
                deletions_by_chat[chat_id].append(msg_id)

        # Process the deletions for each affected chat.
        for chat_id, ids_to_delete in deletions_by_chat.items():
            mark_as_deleted(chat_id, ids_to_delete)

    # --- 3. Strategy for Outgoing Messages (User vs. Bot) ---
    if await borg.is_bot():
        # BOT MODE: Monkey-patch send methods.
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
                messages = result if isinstance(result, list) else [result]
                for sent_message in messages:
                    if sent_message:
                        add_message(
                            sent_message.chat_id, sent_message.id, sent_message.date
                        )
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
