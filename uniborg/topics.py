# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.
"""Topics in private chats (threaded mode) and in forum supergroups.

Terms used below:

- A *topic root* is the `MessageActionTopicCreate` service message that opens
  a topic.
- A *private topic* is a topic in a bot's private chat, which BotFather calls
  threaded mode. Every message in one carries a reply header with
  `forum_topic` set and the *topic id* in `reply_to_top_id`. That id comes
  from the user's message box: it is not the id of any message in the bot's
  box, the root's included.

This module holds reply detection (`resolve_reply_target`, `is_real_reply`):
whether a message really replies to another one, given that every topic
message has a reply header.

It imports only the standard library and Telethon, so tests and tools can
load it without ``uniborg.util``.
"""
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, Optional, Tuple

from telethon.tl import types

#: How many topic roots `TOPIC_ROOTS` remembers, across all chats.
TOPIC_ROOT_CACHE_SIZE = 4096


class _BoundedMap:
    """A dict that, past MAX_SIZE entries, drops the least recently stored."""

    def __init__(self, *, max_size: int):
        self.max_size = max_size
        self._items: Dict[Tuple[Any, int], Any] = {}

    def __len__(self) -> int:
        return len(self._items)

    def _load(self, key, default):
        return self._items.get(key, default)

    def _store(self, key, value) -> None:
        self._items.pop(key, None)
        self._items[key] = value
        while len(self._items) > self.max_size:
            del self._items[next(iter(self._items))]


class TopicRootCache(_BoundedMap):
    """The id of each topic's root message, keyed by chat and topic.

    In a private chat with topics, the topic id Telegram sends
    (`reply_to_top_id`) comes from the user's message box, so the root's id in
    the bot's own box is only learned by seeing or loading the root once.
    """

    def __init__(self, *, max_size: int = TOPIC_ROOT_CACHE_SIZE):
        super().__init__(max_size=max_size)

    def get(self, chat_id, top_id: int) -> Optional[int]:
        return self._load((chat_id, top_id), None)

    def remember(self, chat_id, top_id: int, *, root_id: int) -> None:
        self._store((chat_id, top_id), root_id)


TOPIC_ROOTS = TopicRootCache()


@dataclass(frozen=True)
class ReplyTarget:
    """The message another message really replies to."""

    msg_id: int
    #: The replied-to message, when it was already at hand or resolving loaded
    #: it, so callers need not load it again.
    message: Optional[Any] = None


def is_topic_root(message) -> bool:
    return isinstance(getattr(message, "action", None), types.MessageActionTopicCreate)


async def resolve_reply_target(
    message,
    *,
    fetch_message: Optional[Callable[[int], Awaitable[Any]]] = None,
    context_messages_by_id: Optional[Dict[int, Any]] = None,
    topic_roots: Optional[TopicRootCache] = None,
) -> Optional[ReplyTarget]:
    """What MESSAGE really replies to, or None when it replies to no message.

    Outside topics this is `reply_to_msg_id`, with no I/O. Inside a topic,
    Telegram gives every message a reply header whose parent is the topic's
    root, so a header pointing at the root is not a reply:

    - In a forum supergroup, a message that replies to nothing has
      `forum_topic` set and no `reply_to_top_id`, and an explicit reply has
      both. The topic id is the root's own id there.
    - In a private chat with topics, every message has `forum_topic` and
      `reply_to_top_id` set, and a message that replies to nothing points
      `reply_to_msg_id` at the root's id in the bot's box. Only the parent's
      action tells the two apart, so the first such message of a topic loads
      its parent, and TOPIC_ROOTS remembers the root for the rest.

    MESSAGE may be a message or an event. The parent comes from
    CONTEXT_MESSAGES_BY_ID when it is there, else from FETCH_MESSAGE, which
    takes a message id in MESSAGE's chat and defaults to
    `message.client.get_messages`. A parent that fails to load counts as a
    real reply, since only a loaded parent can show it is a topic root.
    """
    reply_to_msg_id = getattr(message, "reply_to_msg_id", None)
    if not reply_to_msg_id:
        return None
    parent = (context_messages_by_id or {}).get(reply_to_msg_id)
    header = getattr(message, "reply_to", None)
    if not (isinstance(header, types.MessageReplyHeader) and header.forum_topic):
        return ReplyTarget(msg_id=reply_to_msg_id, message=parent)

    top_id = header.reply_to_top_id
    if top_id is None:
        return None
    if isinstance(getattr(message, "peer_id", None), types.PeerChannel):
        if reply_to_msg_id == top_id:
            return None
        return ReplyTarget(msg_id=reply_to_msg_id, message=parent)

    topic_roots = TOPIC_ROOTS if topic_roots is None else topic_roots
    chat_id = getattr(message, "chat_id", None)
    root_id = topic_roots.get(chat_id, top_id)
    if root_id is not None:
        if reply_to_msg_id == root_id:
            return None
        return ReplyTarget(msg_id=reply_to_msg_id, message=parent)

    if parent is None:
        if fetch_message is None:

            async def fetch_message(msg_id):
                return await message.client.get_messages(chat_id, ids=msg_id)

        try:
            parent = await fetch_message(reply_to_msg_id)
        except Exception:
            parent = None
    if is_topic_root(parent):
        topic_roots.remember(chat_id, top_id, root_id=reply_to_msg_id)
        return None
    return ReplyTarget(msg_id=reply_to_msg_id, message=parent)


async def is_real_reply(
    event,
    *,
    fetch_message: Optional[Callable[[int], Awaitable[Any]]] = None,
    topic_roots: Optional[TopicRootCache] = None,
) -> bool:
    """`event.is_reply`, except that a header which only places EVENT in a
    topic is not a reply. A reply without a message id, such as a story
    reply, still is one."""
    if not event.is_reply:
        return False
    if not getattr(event, "reply_to_msg_id", None):
        return True
    return (
        await resolve_reply_target(
            event, fetch_message=fetch_message, topic_roots=topic_roots
        )
        is not None
    )
