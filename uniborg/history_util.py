# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.

from pynight.common_icecream import ic
import os
import json
import hashlib
from collections import defaultdict, deque
from datetime import datetime, timezone
from telethon import events
from telethon.tl.types import (
    Message,
    MessagePeerReaction,
    MessageReactions,
    PeerChannel,
    PeerChat,
    PeerUser,
    ReactionCount,
    ReactionCustomEmoji,
    ReactionEmoji,
    ReactionPaid,
    UpdateBotMessageReaction,
    UpdateBotMessageReactions,
    UpdateMessageReactions,
)
from telethon import TelegramClient
import telethon.utils
from typing import List, Deque, DefaultDict, Dict, Optional
from dataclasses import dataclass, replace

# Redis utilities
from . import redis_util

# --- Configuration ---
HISTORY_LIMIT = 5000  # Max number of message IDs to store per chat
LAST_N_MAX = HISTORY_LIMIT  # Max number of messages user can request in "last N" mode
GEMINI_FILE_CACHE_DURATION = 47 * 3600  # 47 hours, just under the 48h expiry

# Free-tier Gemini keys have a per-model cached-content storage limit of 0, so context
# caching for that (key, model) pair permanently 429s. Once detected we stop caching for
# it; the TTL is a re-probe window in case the key is later upgraded to a paid tier.
GEMINI_CACHE_DISABLED_DURATION = 30 * 24 * 3600  # 30 days
REACTION_HISTORY_CACHE_VERBOSITY_MODE = os.environ.get(
    "REACTION_HISTORY_CACHE_VERBOSITY_MODE", "silent"
)
REACTION_CACHE_DEBUG = True


# --- Data Structures ---


@dataclass(frozen=True)
class HistoryItem:
    """Represents a single message entry in our history cache."""

    message_id: int
    timestamp: datetime
    deleted: bool = False
    reactions: Optional[dict] = None

    def to_dict(self) -> dict:
        """Convert to dictionary for Redis storage."""
        return {
            "message_id": self.message_id,
            "timestamp": self.timestamp.isoformat(),
            "deleted": self.deleted,
            "reactions": self.reactions,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "HistoryItem":
        """Create from dictionary retrieved from Redis."""
        return cls(
            message_id=data["message_id"],
            timestamp=datetime.fromisoformat(data["timestamp"]),
            deleted=data.get("deleted", False),
            reactions=data.get("reactions"),
        )


# --- Redis Connection Delegation ---


# --- Reaction Serialization and Debugging ---


def _reaction_history_cache_print_enabled(*modes):
    mode = (REACTION_HISTORY_CACHE_VERBOSITY_MODE or "silent").lower()
    return bool(REACTION_CACHE_DEBUG and mode in modes)


def _reaction_history_cache_print(label, **payload):
    if not _reaction_history_cache_print_enabled("print_each_update", "debug", "all"):
        return

    print(
        "HistoryUtil reaction_history_cache "
        + label
        + ": "
        + json.dumps(payload, ensure_ascii=False, default=str, sort_keys=True),
        flush=True,
    )


def _peer_to_dict(peer) -> Optional[dict]:
    if peer is None:
        return None
    out = {"type": type(peer).__name__}
    for attr in ("user_id", "chat_id", "channel_id"):
        value = getattr(peer, attr, None)
        if value is not None:
            out[attr] = int(value)
    try:
        out["peer_id"] = telethon.utils.get_peer_id(peer)
    except Exception:
        pass
    return out


def _peer_from_dict(data: Optional[dict]):
    if not data:
        return None
    if data.get("user_id") is not None:
        return PeerUser(int(data["user_id"]))
    if data.get("chat_id") is not None:
        return PeerChat(int(data["chat_id"]))
    if data.get("channel_id") is not None:
        return PeerChannel(int(data["channel_id"]))
    peer_id = data.get("peer_id")
    if peer_id is not None:
        peer_id = int(peer_id)
        if str(peer_id).startswith("-100"):
            return PeerChannel(int(str(peer_id)[4:]))
        if peer_id < 0:
            return PeerChat(abs(peer_id))
        return PeerUser(peer_id)
    return None


def _peer_cache_id(peer) -> Optional[int]:
    try:
        return int(telethon.utils.get_peer_id(peer))
    except Exception:
        value = (
            getattr(peer, "channel_id", None)
            or getattr(peer, "chat_id", None)
            or getattr(peer, "user_id", None)
        )
        return int(value) if value is not None else None


def _reaction_to_dict(reaction) -> Optional[dict]:
    if reaction is None:
        return None
    if hasattr(reaction, "emoticon"):
        return {"type": "emoji", "emoticon": getattr(reaction, "emoticon", None)}
    if hasattr(reaction, "document_id"):
        return {"type": "custom_emoji", "document_id": int(reaction.document_id)}
    if isinstance(reaction, ReactionPaid):
        return {"type": "paid"}
    return {"type": type(reaction).__name__, "repr": repr(reaction)}


def _reaction_from_dict(data: Optional[dict]):
    if not data:
        return None
    kind = data.get("type")
    if kind == "emoji":
        return ReactionEmoji(data.get("emoticon") or "")
    if kind == "custom_emoji":
        return ReactionCustomEmoji(int(data.get("document_id") or 0))
    if kind == "paid":
        return ReactionPaid()
    return ReactionEmoji(data.get("repr") or "?")


def _reaction_signature(reaction) -> tuple:
    if hasattr(reaction, "emoticon"):
        return (type(reaction).__name__, getattr(reaction, "emoticon", None))
    if hasattr(reaction, "document_id"):
        return (type(reaction).__name__, getattr(reaction, "document_id", None))
    return (type(reaction).__name__, repr(reaction))


def _reaction_key_to_reaction(reaction_by_key: dict, reaction):
    key = _reaction_signature(reaction)
    reaction_by_key.setdefault(key, reaction)
    return key


def message_reactions_to_dict(reactions) -> Optional[dict]:
    if reactions is None:
        return None
    return {
        "min": getattr(reactions, "min", None),
        "can_see_list": getattr(reactions, "can_see_list", None),
        "reactions_as_tags": getattr(reactions, "reactions_as_tags", None),
        "results": [
            {
                "reaction": _reaction_to_dict(getattr(result, "reaction", None)),
                "count": getattr(result, "count", None),
                "chosen_order": getattr(result, "chosen_order", None),
            }
            for result in (getattr(reactions, "results", None) or [])
        ],
        "recent_reactions": [
            {
                "peer_id": _peer_to_dict(getattr(entry, "peer_id", None)),
                "date": entry.date.isoformat() if getattr(entry, "date", None) else None,
                "reaction": _reaction_to_dict(getattr(entry, "reaction", None)),
                "big": getattr(entry, "big", None),
                "unread": getattr(entry, "unread", None),
                "my": getattr(entry, "my", None),
            }
            for entry in (getattr(reactions, "recent_reactions", None) or [])
        ],
    }


def message_reactions_from_dict(data: Optional[dict]):
    if not data:
        return None
    results = []
    for item in data.get("results") or []:
        reaction = _reaction_from_dict(item.get("reaction"))
        if reaction is None:
            continue
        results.append(
            ReactionCount(
                reaction=reaction,
                count=int(item.get("count") or 0),
                chosen_order=item.get("chosen_order"),
            )
        )

    recent_reactions = []
    for item in data.get("recent_reactions") or []:
        peer = _peer_from_dict(item.get("peer_id"))
        reaction = _reaction_from_dict(item.get("reaction"))
        if peer is None or reaction is None:
            continue
        date = None
        if item.get("date"):
            try:
                date = datetime.fromisoformat(item["date"])
            except Exception:
                date = None
        recent_reactions.append(
            MessagePeerReaction(
                peer_id=peer,
                date=date,
                reaction=reaction,
                big=item.get("big"),
                unread=item.get("unread"),
                my=item.get("my"),
            )
        )

    return MessageReactions(
        results=results,
        min=data.get("min"),
        can_see_list=data.get("can_see_list"),
        reactions_as_tags=data.get("reactions_as_tags"),
        recent_reactions=recent_reactions or None,
    )


def _reaction_summary_value(reaction):
    if reaction is None:
        return None
    if hasattr(reaction, "emoticon"):
        return getattr(reaction, "emoticon", None)
    if hasattr(reaction, "document_id"):
        return f"custom:{getattr(reaction, 'document_id', None)}"
    return type(reaction).__name__


def _reaction_count_summary(results):
    return [
        {
            "reaction": _reaction_summary_value(getattr(result, "reaction", None)),
            "count": getattr(result, "count", None),
            "chosen_order": getattr(result, "chosen_order", None),
        }
        for result in (results or [])
    ]


def _message_reactions_summary(reactions):
    if reactions is None:
        return None
    recent = getattr(reactions, "recent_reactions", None) or []
    return {
        "type": type(reactions).__name__,
        "min": getattr(reactions, "min", None),
        "can_see_list": getattr(reactions, "can_see_list", None),
        "results": _reaction_count_summary(getattr(reactions, "results", None)),
        "recent_count": len(recent),
        "recent": [
            {
                "peer": _peer_to_dict(getattr(entry, "peer_id", None)),
                "reaction": _reaction_summary_value(getattr(entry, "reaction", None)),
            }
            for entry in recent[:10]
        ],
    }


def _reaction_update_summary(update, chat_id=None, reactions=None):
    return {
        "update_type": type(update).__name__,
        "cache_key": [chat_id, getattr(update, "msg_id", None)] if chat_id is not None else None,
        "peer": _peer_to_dict(getattr(update, "peer", None)),
        "msg_id": getattr(update, "msg_id", None),
        "actor": _peer_to_dict(getattr(update, "actor", None)),
        "old_reactions": [_reaction_summary_value(r) for r in (getattr(update, "old_reactions", None) or [])],
        "new_reactions": [_reaction_summary_value(r) for r in (getattr(update, "new_reactions", None) or [])],
        "aggregate_update_reactions": _reaction_count_summary(getattr(update, "reactions", None)),
        "cached_reactions": _message_reactions_summary(reactions),
    }


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


# --- Storage Backend Functions ---


async def _add_message_redis(chat_id: int, message_id: int, timestamp: datetime):
    """Add message to Redis storage, preserving any cached metadata."""
    existing_reactions = await _get_message_reactions_redis(chat_id, message_id)
    item = HistoryItem(
        message_id=message_id,
        timestamp=timestamp,
        reactions=existing_reactions,
    )
    await _upsert_history_item_redis(chat_id, item)


def _add_message_memory(chat_id: int, message_id: int, timestamp: datetime):
    """Add message to in-memory storage (fallback), preserving metadata."""
    existing_reactions = _get_message_reactions_memory(chat_id, message_id)
    _upsert_history_item_memory(
        chat_id,
        HistoryItem(
            message_id=message_id,
            timestamp=timestamp,
            reactions=existing_reactions,
        ),
    )


async def _mark_deleted_redis(chat_id: int, message_ids: List[int]):
    """Mark messages as deleted in Redis storage."""
    redis_client = await redis_util.get_redis()
    if not redis_client:
        return False

    try:
        # Get current history
        history_key = redis_util.chat_history_key(chat_id)
        raw_items = await redis_client.zrange(history_key, 0, -1)

        if not raw_items:
            return True

        pipe = redis_client.pipeline()
        message_ids_set = set(message_ids)

        # Remove all items and re-add with updated deleted status
        pipe.delete(history_key)

        for raw_item in raw_items:
            item_data = json.loads(raw_item)
            item = HistoryItem.from_dict(item_data)
            if item.message_id in message_ids_set:
                item = replace(item, deleted=True)
            pipe.zadd(
                history_key, {json.dumps(item.to_dict()): item.timestamp.timestamp()}
            )

        pipe.expire(history_key, redis_util.get_very_long_expire_duration())
        await pipe.execute()
        return True
    except Exception as e:
        print(f"HistoryUtil: Redis mark_deleted failed: {e}")
        return False


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


async def _get_history_items_redis(chat_id: int) -> List[HistoryItem]:
    """Get all history items from Redis."""
    raw_items = await redis_util.zrange_and_renew(
        redis_util.chat_history_key(chat_id),
        expire_seconds=redis_util.get_very_long_expire_duration(),
    )

    items = []
    for raw_item in raw_items:
        try:
            item_data = json.loads(raw_item)
            items.append(HistoryItem.from_dict(item_data))
        except (json.JSONDecodeError, KeyError):
            print(f"HistoryUtil: Corrupted history item in Redis:\n{raw_item}")
            continue  # Skip corrupted entries

    return items


def _get_history_items_memory(chat_id: int) -> List[HistoryItem]:
    """Get all history items from memory storage."""
    return list(_history_cache.get(chat_id, deque()))


def _trim_history_items(items: List[HistoryItem]) -> List[HistoryItem]:
    items = sorted(items, key=lambda item: item.timestamp.timestamp())
    return items[-HISTORY_LIMIT:]


async def _write_history_items_redis(chat_id: int, items: List[HistoryItem]) -> bool:
    redis_client = await redis_util.get_redis()
    if not redis_client:
        return False
    history_key = redis_util.chat_history_key(chat_id)
    items = _trim_history_items(items)
    try:
        pipe = redis_client.pipeline()
        pipe.delete(history_key)
        if items:
            pipe.zadd(
                history_key,
                {
                    json.dumps(item.to_dict()): item.timestamp.timestamp()
                    for item in items
                },
            )
        pipe.expire(history_key, redis_util.get_very_long_expire_duration())
        await pipe.execute()
        return True
    except Exception as e:
        print(f"HistoryUtil: Redis write history failed: {e}")
        return False


async def _upsert_history_item_redis(chat_id: int, new_item: HistoryItem) -> bool:
    items = [
        item
        for item in await _get_history_items_redis(chat_id)
        if item.message_id != new_item.message_id
    ]
    items.append(new_item)
    if not await _write_history_items_redis(chat_id, items):
        return False
    await redis_util.set_with_expiry(
        redis_util.message_lookup_key(new_item.message_id),
        str(chat_id),
        expire_seconds=redis_util.get_very_long_expire_duration(),
    )
    return True


def _upsert_history_item_memory(chat_id: int, new_item: HistoryItem):
    items = [
        item
        for item in _get_history_items_memory(chat_id)
        if item.message_id != new_item.message_id
    ]
    items.append(new_item)
    updated = EvictionTrackingDeque(maxlen=HISTORY_LIMIT)
    for item in _trim_history_items(items):
        updated.append(item)
        _message_id_to_chat_id_map[item.message_id] = chat_id
    _history_cache[chat_id] = updated


def _get_message_reactions_memory(chat_id: int, message_id: int) -> Optional[dict]:
    for item in _get_history_items_memory(chat_id):
        if item.message_id == message_id:
            return item.reactions
    return None


async def _get_message_reactions_redis(chat_id: int, message_id: int) -> Optional[dict]:
    for item in await _get_history_items_redis(chat_id):
        if item.message_id == message_id:
            return item.reactions
    return None


# --- Public API ---


async def add_message(chat_id: int, message_id: int, timestamp: datetime):
    """Adds a new message to the history storage."""
    if redis_util.is_redis_available():
        try:
            await _add_message_redis(chat_id, message_id, timestamp)
            return
        except Exception as e:
            print(f"HistoryUtil: Redis add_message failed, falling back to memory: {e}")

    # Fallback to memory storage
    if redis_util.FALLBACK_TO_MEMORY:
        _add_message_memory(chat_id, message_id, timestamp)


async def mark_as_deleted(chat_id: int, message_ids: List[int]):
    """Marks a list of message IDs as deleted for a specific chat."""
    if redis_util.is_redis_available():
        try:
            success = await _mark_deleted_redis(chat_id, message_ids)
            if success:
                return
        except Exception as e:
            print(
                f"HistoryUtil: Redis mark_as_deleted failed, falling back to memory: {e}"
            )

    # Fallback to memory storage
    if redis_util.FALLBACK_TO_MEMORY:
        _mark_deleted_memory(chat_id, message_ids)


async def get_last_n_ids(
    chat_id: int, n: int, skip_deleted_p: bool = True
) -> List[int]:
    """Retrieves the last N message IDs for a given chat."""
    if redis_util.is_redis_available():
        try:
            items = await _get_history_items_redis(chat_id)
            if skip_deleted_p:
                filtered_items = [item for item in items if not item.deleted]
                return [item.message_id for item in filtered_items[-n:]]
            else:
                return [item.message_id for item in items[-n:]]
        except Exception as e:
            print(
                f"HistoryUtil: Redis get_last_n_ids failed, falling back to memory: {e}"
            )

    # Fallback to memory storage
    if redis_util.FALLBACK_TO_MEMORY:
        items = _get_history_items_memory(chat_id)
        if skip_deleted_p:
            filtered_ids = [item.message_id for item in items if not item.deleted]
            return filtered_ids[-n:]
        else:
            return [item.message_id for item in items[-n:]]

    return []


async def get_all_ids(chat_id: int, skip_deleted_p: bool = True) -> List[int]:
    """Retrieves all cached message IDs for a given chat."""
    if redis_util.is_redis_available():
        try:
            items = await _get_history_items_redis(chat_id)
            if skip_deleted_p:
                return [item.message_id for item in items if not item.deleted]
            else:
                return [item.message_id for item in items]
        except Exception as e:
            print(f"HistoryUtil: Redis get_all_ids failed, falling back to memory: {e}")

    # Fallback to memory storage
    if redis_util.FALLBACK_TO_MEMORY:
        items = _get_history_items_memory(chat_id)
        if skip_deleted_p:
            return [item.message_id for item in items if not item.deleted]
        else:
            return [item.message_id for item in items]

    return []


async def get_ids_since(
    chat_id: int,
    timestamp: datetime,
    skip_deleted_p: bool = True,
) -> List[int]:
    """Retrieves message IDs for a chat that have occurred since the given timestamp."""
    if redis_util.is_redis_available():
        try:
            items = await _get_history_items_redis(chat_id)
            # ic(chat_id, items)

            if skip_deleted_p:
                if False:
                    #: for debugging
                    for item in items:
                        if item.message_id > 5625:
                            ic(
                                item.__dict__,
                                item.timestamp,
                                timestamp,
                                item.timestamp > timestamp,
                            )

                result = [
                    item.message_id
                    for item in items
                    if item.timestamp > timestamp and not item.deleted
                ]
                return result
            else:
                return [item.message_id for item in items if item.timestamp > timestamp]
        except Exception as e:
            print(
                f"HistoryUtil: Redis get_ids_since failed, falling back to memory: {e}"
            )

    # Fallback to memory storage
    if redis_util.FALLBACK_TO_MEMORY:
        items = _get_history_items_memory(chat_id)
        if skip_deleted_p:
            return [
                item.message_id
                for item in items
                if item.timestamp > timestamp and not item.deleted
            ]
        else:
            return [item.message_id for item in items if item.timestamp > timestamp]

    return []


async def clear_chat_history(chat_id: int):
    """Clears the history for a specific chat."""
    if redis_util.is_redis_available():
        try:
            await redis_util.delete_key(redis_util.chat_history_key(chat_id))
            return
        except Exception as e:
            print(
                f"HistoryUtil: Redis clear_chat_history failed, falling back to memory: {e}"
            )

    # Fallback to memory storage
    if redis_util.FALLBACK_TO_MEMORY:
        if chat_id in _history_cache:
            _history_cache[chat_id].clear()


# --- Reaction History API ---


def _reaction_counts_from_results(results):
    reactions_by_key = {}
    counts_by_key = {}
    for result in results or []:
        reaction = getattr(result, "reaction", None)
        if reaction is None:
            continue
        key = _reaction_key_to_reaction(reactions_by_key, reaction)
        counts_by_key[key] = counts_by_key.get(key, 0) + int(
            getattr(result, "count", 0) or 0
        )
    return reactions_by_key, counts_by_key


def _reaction_results_from_counts(reactions_by_key, counts_by_key):
    return [
        ReactionCount(reaction=reactions_by_key[key], count=count)
        for key, count in counts_by_key.items()
        if count > 0
    ]


def _recent_without_actor(recent_reactions, actor):
    actor_id = _peer_cache_id(actor)
    if actor_id is None:
        return list(recent_reactions or [])
    return [
        entry
        for entry in (recent_reactions or [])
        if _peer_cache_id(getattr(entry, "peer_id", None)) != actor_id
    ]


def _message_reactions_from_recent(recent_reactions):
    reactions_by_key = {}
    counts_by_key = {}
    for entry in recent_reactions or []:
        reaction = getattr(entry, "reaction", None)
        if reaction is None:
            continue
        key = _reaction_key_to_reaction(reactions_by_key, reaction)
        counts_by_key[key] = counts_by_key.get(key, 0) + 1
    return MessageReactions(
        results=_reaction_results_from_counts(reactions_by_key, counts_by_key),
        can_see_list=bool(recent_reactions),
        recent_reactions=list(recent_reactions or []) or None,
    )


def _merge_individual_reaction_update(existing, update):
    recent = _recent_without_actor(
        getattr(existing, "recent_reactions", None), getattr(update, "actor", None)
    )
    actor = getattr(update, "actor", None)
    for reaction in getattr(update, "new_reactions", None) or []:
        if actor is not None:
            recent.append(
                MessagePeerReaction(
                    peer_id=actor,
                    date=getattr(update, "date", None),
                    reaction=reaction,
                )
            )

    if existing is not None and getattr(existing, "results", None):
        reactions_by_key, counts_by_key = _reaction_counts_from_results(existing.results)
        for reaction in getattr(update, "old_reactions", None) or []:
            key = _reaction_key_to_reaction(reactions_by_key, reaction)
            counts_by_key[key] = counts_by_key.get(key, 0) - 1
        for reaction in getattr(update, "new_reactions", None) or []:
            key = _reaction_key_to_reaction(reactions_by_key, reaction)
            counts_by_key[key] = counts_by_key.get(key, 0) + 1
        return MessageReactions(
            results=_reaction_results_from_counts(reactions_by_key, counts_by_key),
            can_see_list=bool(recent),
            recent_reactions=recent or None,
        )

    return _message_reactions_from_recent(recent)


def _merge_aggregate_reaction_update(existing, update):
    recent = list(getattr(existing, "recent_reactions", None) or [])
    return MessageReactions(
        results=list(getattr(update, "reactions", None) or []),
        can_see_list=bool(recent),
        recent_reactions=recent or None,
    )


async def record_message_reactions(
    chat_id: int, message_id: int, reactions, updated_at: Optional[datetime] = None
):
    """Persist reactions as metadata for a cached message history item."""
    if reactions is None:
        return False
    updated_at = updated_at or datetime.now(timezone.utc)
    reactions_data = message_reactions_to_dict(reactions)
    if redis_util.is_redis_available():
        try:
            items = await _get_history_items_redis(chat_id)
            existing = next(
                (item for item in items if item.message_id == message_id), None
            )
            item = HistoryItem(
                message_id=message_id,
                timestamp=existing.timestamp if existing else updated_at,
                deleted=existing.deleted if existing else False,
                reactions=reactions_data,
            )
            if await _upsert_history_item_redis(chat_id, item):
                return True
        except Exception as e:
            print(
                "HistoryUtil: Redis record_message_reactions failed, "
                f"falling back to memory: {e}"
            )
    if redis_util.FALLBACK_TO_MEMORY:
        existing = next(
            (
                item
                for item in _get_history_items_memory(chat_id)
                if item.message_id == message_id
            ),
            None,
        )
        _upsert_history_item_memory(
            chat_id,
            HistoryItem(
                message_id=message_id,
                timestamp=existing.timestamp if existing else updated_at,
                deleted=existing.deleted if existing else False,
                reactions=reactions_data,
            ),
        )
        return True
    return False


async def get_message_reactions(chat_id: int, message_id: int):
    """Return cached MessageReactions for a message, if available."""
    if redis_util.is_redis_available():
        try:
            data = await _get_message_reactions_redis(chat_id, message_id)
            if data:
                return message_reactions_from_dict(data)
        except Exception as e:
            print(
                "HistoryUtil: Redis get_message_reactions failed, "
                f"falling back to memory: {e}"
            )
    if redis_util.FALLBACK_TO_MEMORY:
        return message_reactions_from_dict(
            _get_message_reactions_memory(chat_id, message_id)
        )
    return None


async def hydrate_message_reactions(chat_id: int, messages: List[Message]) -> int:
    """Attach cached reactions to message objects that do not already have them."""
    if not messages:
        return 0
    reactions_by_id = {}
    if redis_util.is_redis_available():
        try:
            reactions_by_id = {
                item.message_id: item.reactions
                for item in await _get_history_items_redis(chat_id)
                if item.reactions
            }
        except Exception as e:
            print(
                "HistoryUtil: Redis hydrate_message_reactions failed, "
                f"falling back to memory: {e}"
            )
    if not reactions_by_id and redis_util.FALLBACK_TO_MEMORY:
        reactions_by_id = {
            item.message_id: item.reactions
            for item in _get_history_items_memory(chat_id)
            if item.reactions
        }
    applied = 0
    for message in messages:
        msg_id = getattr(message, "id", None)
        if msg_id is None or getattr(message, "reactions", None):
            continue
        reactions = message_reactions_from_dict(reactions_by_id.get(msg_id))
        if reactions is not None:
            message.reactions = reactions
            applied += 1
    if applied:
        _reaction_history_cache_print(
            "cached_reactions_applied_to_history",
            chat_id=chat_id,
            applied=applied,
            message_ids=[getattr(message, "id", None) for message in messages or []],
        )
    return applied


async def record_reaction_update(update):
    """Record a raw Telegram reaction update into message history metadata."""
    try:
        chat_id = telethon.utils.get_peer_id(getattr(update, "peer", None))
    except Exception:
        chat_id = None
    msg_id = getattr(update, "msg_id", None)
    if chat_id is None or msg_id is None:
        _reaction_history_cache_print(
            "reaction_update_received_but_not_cached",
            update=_reaction_update_summary(update),
            reason="missing peer/msg_id cache key",
        )
        return False
    existing = await get_message_reactions(chat_id, msg_id)
    if isinstance(update, UpdateMessageReactions):
        reactions = getattr(update, "reactions", None)
    elif isinstance(update, UpdateBotMessageReaction):
        reactions = _merge_individual_reaction_update(existing, update)
    elif isinstance(update, UpdateBotMessageReactions):
        reactions = _merge_aggregate_reaction_update(existing, update)
    else:
        _reaction_history_cache_print(
            "reaction_update_received_but_not_cached",
            update=_reaction_update_summary(update, chat_id),
            reason="unsupported update type",
        )
        return False
    await record_message_reactions(
        chat_id, msg_id, reactions, updated_at=getattr(update, "date", None)
    )
    _reaction_history_cache_print(
        "reaction_update_cached",
        update=_reaction_update_summary(update, chat_id, reactions),
    )
    return True


# --- File Caching API ---


async def cache_file(
    file_id: str,
    data: str,
    *,
    data_storage_type: str,
    filename: str = None,
    mime_type: str = None,
) -> bool:
    """
    Cache file data with metadata in Redis. Data is expected to be a string
    (either raw text or Base64 encoded).
    """
    field_values = {
        "data": data,
        "data_storage_type": data_storage_type,
        "cached_at": datetime.now().isoformat(),
    }

    if filename:
        field_values["filename"] = filename
    if mime_type:
        field_values["mime_type"] = mime_type

    return await redis_util.hset_with_expiry(
        redis_util.file_cache_key(file_id), field_values
    )


async def get_cached_file(file_id: str) -> Optional[dict]:
    """
    Get cached file data with metadata from Redis. Returns the raw hash dictionary.
    The caller is responsible for interpreting the 'data' field based on
    'data_storage_type'.
    """
    cached_data = await redis_util.hgetall_and_renew(redis_util.file_cache_key(file_id))
    if cached_data and "data" in cached_data and "data_storage_type" in cached_data:
        # The data is already decoded from bytes to string by the redis client.
        # We return the whole dictionary for the caller to process.
        return cached_data
    return None


async def cache_gemini_file_info(
    file_id: str, user_id: int, name: str, uri: str, mime_type: str
) -> bool:
    """Cache a Gemini File API file's name, URI and MIME type for a specific user."""
    field_values = {"name": name, "uri": uri, "mime_type": mime_type}
    return await redis_util.hset_with_expiry(
        redis_util.gemini_file_cache_key(file_id, user_id),
        field_values,
        expire_seconds=GEMINI_FILE_CACHE_DURATION,
    )


async def get_cached_gemini_file_info(file_id: str, user_id: int) -> Optional[dict]:
    """Get a cached Gemini File API file's info for a specific user without renewing expiry."""
    return await redis_util.hgetall_and_renew(
        redis_util.gemini_file_cache_key(file_id, user_id),
        expire_seconds=GEMINI_FILE_CACHE_DURATION,
        renew=False,
    )


def _gemini_api_key_hash(api_key: str) -> str:
    """Short, stable hash of an API key for use in cache-state Redis keys (never the raw key)."""
    return hashlib.sha256(api_key.encode("utf-8")).hexdigest()[:32]


async def is_gemini_caching_disabled(api_key: str, model: str) -> bool:
    """Whether context caching has been disabled for this (api key, model) pair.

    Returns False when no api_key/model is given or Redis is unavailable, so caching
    is attempted by default and the worst case is a recoverable one-time 429.
    """
    if not api_key or not model:
        return False
    key = redis_util.gemini_cache_disabled_key(_gemini_api_key_hash(api_key), model)
    return await redis_util.get_and_renew(key, renew=False) is not None


async def disable_gemini_caching(api_key: str, model: str) -> bool:
    """Mark a (api key, model) pair as unable to use context caching (free-tier quota=0)."""
    if not api_key or not model:
        return False
    key = redis_util.gemini_cache_disabled_key(_gemini_api_key_hash(api_key), model)
    return await redis_util.set_with_expiry(
        key, "1", expire_seconds=GEMINI_CACHE_DISABLED_DURATION
    )


# --- Automatic History Population ---


async def _lookup_chat_id_for_deleted_message(message_id: int) -> Optional[int]:
    """Look up chat_id for a deleted message, trying Redis first."""
    if redis_util.is_redis_available():
        try:
            # No need to renew expiry on deletion lookup
            chat_id_str = await redis_util.get_and_renew(
                redis_util.message_lookup_key(message_id),
                expire_seconds=redis_util.get_very_long_expire_duration(),
                renew=False,
            )
            if chat_id_str:
                return int(chat_id_str)
        except Exception as e:
            print(f"HistoryUtil: Redis lookup failed: {e}")

    # Fallback to memory lookup
    return _message_id_to_chat_id_map.get(message_id)


original_send_message = None
original_send_file = None


async def initialize_history_handler():
    """
    Initializes history tracking. It uses event handlers and monkey-patching
    to log new, outgoing, and deleted messages.
    """
    global borg, original_send_message, original_send_file
    if not borg:
        print("HistoryUtil Error: borg client is not set. Cannot initialize.")
        return

    if hasattr(borg, "_history_patched"):
        # Remove old event handlers and add new ones instead of returning
        borg.remove_events_of_mod(__name__)

        assert original_send_file is not None and original_send_message is not None
    else:
        # Store the original methods before we replace them
        original_send_message = borg.send_message
        original_send_file = borg.send_file

    borg._history_patched = True

    # --- 1. Handler for Incoming Messages ---
    @borg.on(events.NewMessage(incoming=True))
    async def incoming_message_recorder(event: events.NewMessage.Event):
        # print(f"History: new message in {event.chat_id}: {event.id}, text (truncated):\n{event.text[:100]}")

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

    # --- 3. Handler for Reaction Updates ---
    @borg.on(events.Raw(UpdateMessageReactions))
    async def message_reactions_recorder(event):
        await record_reaction_update(getattr(event, "original_update", event))

    @borg.on(events.Raw(UpdateBotMessageReaction))
    async def bot_message_reaction_recorder(event):
        await record_reaction_update(getattr(event, "original_update", event))

    @borg.on(events.Raw(UpdateBotMessageReactions))
    async def bot_message_reactions_recorder(event):
        await record_reaction_update(getattr(event, "original_update", event))

    # --- 3. Strategy for Outgoing Messages (User vs. Bot) ---
    if await borg.is_bot():
        # BOT MODE: Monkey-patch send methods.
        async def patched_send_message(*args, **kwargs):
            # Call the original function to actually send the message
            sent_message: Message = await original_send_message(*args, **kwargs)
            # After the message is sent, log its ID
            if sent_message:
                await add_message(
                    sent_message.chat_id, sent_message.id, sent_message.date
                )
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
