# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.

import os
import json
import asyncio
from collections import defaultdict, deque
from datetime import datetime, timedelta
from telethon import events
from telethon.tl.types import Message
from telethon import TelegramClient
import telethon.utils
from typing import List, Deque, DefaultDict, Dict, Optional
from dataclasses import dataclass, replace, asdict

try:
    import redis.asyncio as redis
    REDIS_AVAILABLE = True
except ImportError:
    redis = None
    REDIS_AVAILABLE = False

# --- Configuration ---
REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379")
REDIS_EXPIRE_DURATION = int(os.environ.get("BORG_REDIS_EXPIRE_DURATION", "3600"))  # 1 hour default
HISTORY_LIMIT = 2000  # Max number of message IDs to store per chat
FALLBACK_TO_MEMORY = True  # Fallback to in-memory storage if Redis fails

# --- Data Structures ---

@dataclass(frozen=True)
class HistoryItem:
    """Represents a single message entry in our history cache."""
    message_id: int
    timestamp: datetime
    deleted: bool = False
    
    def to_dict(self) -> dict:
        """Convert to dictionary for Redis storage."""
        return {
            "message_id": self.message_id,
            "timestamp": self.timestamp.isoformat(),
            "deleted": self.deleted
        }
    
    @classmethod
    def from_dict(cls, data: dict) -> "HistoryItem":
        """Create from dictionary retrieved from Redis."""
        return cls(
            message_id=data["message_id"],
            timestamp=datetime.fromisoformat(data["timestamp"]),
            deleted=data.get("deleted", False)
        )

# --- Redis Connection Management ---
_redis_client: Optional[redis.Redis] = None

async def _get_redis() -> Optional[redis.Redis]:
    """Get or create Redis connection."""
    global _redis_client
    if not REDIS_AVAILABLE:
        return None
        
    if _redis_client is None:
        try:
            _redis_client = redis.from_url(REDIS_URL, decode_responses=True)
            # Test connection
            await _redis_client.ping()
        except Exception as e:
            print(f"HistoryUtil: Failed to connect to Redis: {e}")
            if not FALLBACK_TO_MEMORY:
                raise
            return None
    
    return _redis_client

# --- Fallback In-Memory Storage (original implementation) ---
class EvictionTrackingDeque(deque):
    """A deque subclass that automatically removes evicted items from the lookup map."""
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

    def append(self, item: HistoryItem):
        if self.maxlen is not None and len(self) == self.maxlen:
            evicted_item = self[0]
            _message_id_to_chat_id_map.pop(evicted_item.message_id, None)
        super().append(item)

    def clear(self):
        for item in self:
            _message_id_to_chat_id_map.pop(item.message_id, None)
        super().clear()

# Global fallback storage
_message_id_to_chat_id_map: Dict[int, int] = {}
_history_cache: DefaultDict[int, Deque[HistoryItem]] = defaultdict(
    lambda: EvictionTrackingDeque(maxlen=HISTORY_LIMIT)
)

# --- Client Instance ---
# The borg client instance will be populated by `_async_init` in `uniborg/uniborg.py`.
borg: TelegramClient = None


# --- Redis Keys ---
def _chat_history_key(chat_id: int) -> str:
    """Redis key for chat history list."""
    return f"borg:history:chat:{chat_id}"

def _message_lookup_key(message_id: int) -> str:
    """Redis key for message to chat ID lookup."""
    return f"borg:history:lookup:{message_id}"

def _file_cache_key(file_id: str) -> str:
    """Redis key for file cache."""
    return f"borg:files:{file_id}"

# --- Storage Backend Functions ---

async def _add_message_redis(redis_client: redis.Redis, chat_id: int, message_id: int, timestamp: datetime):
    """Add message to Redis storage."""
    item = HistoryItem(message_id=message_id, timestamp=timestamp)
    pipe = redis_client.pipeline()
    
    # Add to chat history (as a sorted set for efficient operations)
    pipe.zadd(_chat_history_key(chat_id), {json.dumps(item.to_dict()): timestamp.timestamp()})
    # Maintain size limit by removing oldest entries
    pipe.zremrangebyrank(_chat_history_key(chat_id), 0, -(HISTORY_LIMIT + 1))
    # Add lookup mapping
    pipe.set(_message_lookup_key(message_id), chat_id, ex=REDIS_EXPIRE_DURATION)
    # Set expiry for chat history
    pipe.expire(_chat_history_key(chat_id), REDIS_EXPIRE_DURATION)
    
    await pipe.execute()

def _add_message_memory(chat_id: int, message_id: int, timestamp: datetime):
    """Add message to in-memory storage (fallback)."""
    _history_cache[chat_id].append(HistoryItem(message_id=message_id, timestamp=timestamp))
    _message_id_to_chat_id_map[message_id] = chat_id

async def _mark_deleted_redis(redis_client: redis.Redis, chat_id: int, message_ids: List[int]):
    """Mark messages as deleted in Redis storage."""
    # Get current history
    history_key = _chat_history_key(chat_id)
    raw_items = await redis_client.zrange(history_key, 0, -1)
    
    if not raw_items:
        return
    
    pipe = redis_client.pipeline()
    message_ids_set = set(message_ids)
    
    # Remove all items and re-add with updated deleted status
    pipe.delete(history_key)
    
    for raw_item in raw_items:
        item_data = json.loads(raw_item)
        item = HistoryItem.from_dict(item_data)
        if item.message_id in message_ids_set:
            item = replace(item, deleted=True)
        pipe.zadd(history_key, {json.dumps(item.to_dict()): item.timestamp.timestamp()})
    
    pipe.expire(history_key, REDIS_EXPIRE_DURATION)
    await pipe.execute()

def _mark_deleted_memory(chat_id: int, message_ids: List[int]):
    """Mark messages as deleted in memory storage (fallback)."""
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

async def _get_history_items_redis(redis_client: redis.Redis, chat_id: int) -> List[HistoryItem]:
    """Get all history items from Redis."""
    history_key = _chat_history_key(chat_id)
    raw_items = await redis_client.zrange(history_key, 0, -1)
    await redis_client.expire(history_key, REDIS_EXPIRE_DURATION)  # Renew expiry on access
    
    items = []
    for raw_item in raw_items:
        try:
            item_data = json.loads(raw_item)
            items.append(HistoryItem.from_dict(item_data))
        except (json.JSONDecodeError, KeyError):
            continue  # Skip corrupted entries
    
    return items

def _get_history_items_memory(chat_id: int) -> List[HistoryItem]:
    """Get all history items from memory storage."""
    return list(_history_cache.get(chat_id, deque()))

# --- Public API ---

async def add_message(chat_id: int, message_id: int, timestamp: datetime):
    """Adds a new message to the history storage."""
    redis_client = await _get_redis()
    if redis_client:
        try:
            await _add_message_redis(redis_client, chat_id, message_id, timestamp)
            return
        except Exception as e:
            print(f"HistoryUtil: Redis add_message failed, falling back to memory: {e}")
    
    # Fallback to memory storage
    if FALLBACK_TO_MEMORY:
        _add_message_memory(chat_id, message_id, timestamp)

async def mark_as_deleted(chat_id: int, message_ids: List[int]):
    """Marks a list of message IDs as deleted for a specific chat."""
    redis_client = await _get_redis()
    if redis_client:
        try:
            await _mark_deleted_redis(redis_client, chat_id, message_ids)
            return
        except Exception as e:
            print(f"HistoryUtil: Redis mark_as_deleted failed, falling back to memory: {e}")
    
    # Fallback to memory storage
    if FALLBACK_TO_MEMORY:
        _mark_deleted_memory(chat_id, message_ids)

async def get_last_n_ids(chat_id: int, n: int, skip_deleted_p: bool = True) -> List[int]:
    """Retrieves the last N message IDs for a given chat."""
    redis_client = await _get_redis()
    if redis_client:
        try:
            items = await _get_history_items_redis(redis_client, chat_id)
            if skip_deleted_p:
                filtered_items = [item for item in items if not item.deleted]
                return [item.message_id for item in filtered_items[-n:]]
            else:
                return [item.message_id for item in items[-n:]]
        except Exception as e:
            print(f"HistoryUtil: Redis get_last_n_ids failed, falling back to memory: {e}")
    
    # Fallback to memory storage
    if FALLBACK_TO_MEMORY:
        items = _get_history_items_memory(chat_id)
        if skip_deleted_p:
            filtered_ids = [item.message_id for item in items if not item.deleted]
            return filtered_ids[-n:]
        else:
            return [item.message_id for item in items[-n:]]
    
    return []

async def get_all_ids(chat_id: int, skip_deleted_p: bool = True) -> List[int]:
    """Retrieves all cached message IDs for a given chat."""
    redis_client = await _get_redis()
    if redis_client:
        try:
            items = await _get_history_items_redis(redis_client, chat_id)
            if skip_deleted_p:
                return [item.message_id for item in items if not item.deleted]
            else:
                return [item.message_id for item in items]
        except Exception as e:
            print(f"HistoryUtil: Redis get_all_ids failed, falling back to memory: {e}")
    
    # Fallback to memory storage
    if FALLBACK_TO_MEMORY:
        items = _get_history_items_memory(chat_id)
        if skip_deleted_p:
            return [item.message_id for item in items if not item.deleted]
        else:
            return [item.message_id for item in items]
    
    return []

async def get_ids_since(chat_id: int, timestamp: datetime, skip_deleted_p: bool = True) -> List[int]:
    """Retrieves message IDs for a chat that have occurred since the given timestamp."""
    redis_client = await _get_redis()
    if redis_client:
        try:
            items = await _get_history_items_redis(redis_client, chat_id)
            if skip_deleted_p:
                return [
                    item.message_id for item in items 
                    if item.timestamp > timestamp and not item.deleted
                ]
            else:
                return [item.message_id for item in items if item.timestamp > timestamp]
        except Exception as e:
            print(f"HistoryUtil: Redis get_ids_since failed, falling back to memory: {e}")
    
    # Fallback to memory storage
    if FALLBACK_TO_MEMORY:
        items = _get_history_items_memory(chat_id)
        if skip_deleted_p:
            return [
                item.message_id for item in items
                if item.timestamp > timestamp and not item.deleted
            ]
        else:
            return [item.message_id for item in items if item.timestamp > timestamp]
    
    return []

async def clear_chat_history(chat_id: int):
    """Clears the history for a specific chat."""
    redis_client = await _get_redis()
    if redis_client:
        try:
            await redis_client.delete(_chat_history_key(chat_id))
            return
        except Exception as e:
            print(f"HistoryUtil: Redis clear_chat_history failed, falling back to memory: {e}")
    
    # Fallback to memory storage
    if FALLBACK_TO_MEMORY:
        if chat_id in _history_cache:
            _history_cache[chat_id].clear()

# --- File Caching API ---

async def cache_file(file_id: str, file_data: bytes, *, filename: str = None, mime_type: str = None) -> bool:
    """Cache file data with metadata in Redis with expiry."""
    redis_client = await _get_redis()
    if not redis_client:
        return False
    
    try:
        # Use Redis hash to store file data and metadata efficiently
        pipe = redis_client.pipeline()
        file_key = _file_cache_key(file_id)
        
        # Store binary data directly (no encoding overhead)
        pipe.hset(file_key, "data", file_data)
        
        # Store metadata as strings
        if filename:
            pipe.hset(file_key, "filename", filename)
        if mime_type:
            pipe.hset(file_key, "mime_type", mime_type)
        pipe.hset(file_key, "cached_at", datetime.now().isoformat())
        
        # Set expiration
        pipe.expire(file_key, REDIS_EXPIRE_DURATION)
        
        await pipe.execute()
        return True
    except Exception as e:
        print(f"HistoryUtil: Failed to cache file {file_id}: {e}")
        return False

async def get_cached_file(file_id: str) -> Optional[dict]:
    """Get cached file data with metadata from Redis and renew expiry."""
    redis_client = await _get_redis()
    if not redis_client:
        return None
    
    try:
        file_key = _file_cache_key(file_id)
        
        # Get all hash fields at once
        cached_data = await redis_client.hgetall(file_key)
        if cached_data and "data" in cached_data:
            # Renew expiry on access
            await redis_client.expire(file_key, REDIS_EXPIRE_DURATION)
            
            return {
                "data": cached_data["data"],  # Already bytes from Redis
                "filename": cached_data.get("filename"),
                "mime_type": cached_data.get("mime_type"), 
                "cached_at": cached_data.get("cached_at")
            }
        return None
    except Exception as e:
        print(f"HistoryUtil: Failed to get cached file {file_id}: {e}")
        return None


# --- Automatic History Population ---

async def _lookup_chat_id_for_deleted_message(message_id: int) -> Optional[int]:
    """Look up chat_id for a deleted message, trying Redis first."""
    redis_client = await _get_redis()
    if redis_client:
        try:
            chat_id = await redis_client.get(_message_lookup_key(message_id))
            if chat_id:
                return int(chat_id)
        except Exception as e:
            print(f"HistoryUtil: Redis lookup failed: {e}")
    
    # Fallback to memory lookup
    return _message_id_to_chat_id_map.get(message_id)

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
        await add_message(event.chat_id, event.id, event.date)

    # --- 2. Handler for Deleted Messages ---
    @borg.on(events.MessageDeleted)
    async def message_deleted_recorder(event: events.MessageDeleted.Event):
        if not event.deleted_ids:
            return

        # Group deleted IDs by the chat they belong to.
        deletions_by_chat: DefaultDict[int, List[int]] = defaultdict(list)
        for msg_id in event.deleted_ids:
            # Look up chat_id (Redis first, then memory fallback)
            chat_id = await _lookup_chat_id_for_deleted_message(msg_id)
            if chat_id:
                deletions_by_chat[chat_id].append(msg_id)

        # Process the deletions for each affected chat.
        for chat_id, ids_to_delete in deletions_by_chat.items():
            await mark_as_deleted(chat_id, ids_to_delete)

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
                await add_message(sent_message.chat_id, sent_message.id, sent_message.date)
            return sent_message

        async def patched_send_file(*args, **kwargs):
            # Call the original function
            result = await original_send_file(*args, **kwargs)
            # send_file can return a single Message or a list of Messages (for albums)
            if result:
                messages = result if isinstance(result, list) else [result]
                for sent_message in messages:
                    if sent_message:
                        await add_message(
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
            await add_message(event.chat_id, event.id, event.date)

        print(
            "HistoryUtil (User Mode): Incoming and outgoing message recorders have been activated."
        )
