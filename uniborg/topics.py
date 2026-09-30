# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.
"""Topics in private chats (threaded mode) and in forum supergroups.

Terms used below (docs/private_topics.md has the wire-level detail):

- A *topic root* is the `MessageActionTopicCreate` service message that opens
  a topic.
- A *private topic* is a topic in a bot's private chat, which BotFather calls
  threaded mode. Every message in one carries a reply header with
  `forum_topic` set and the *topic id* in `reply_to_top_id`. That id comes
  from the user's message box: it is not the id of any message in the bot's
  box, the root's included.
- *Placement* is choosing the topic a sent message lands in. In a private
  chat Telegram places a message by `top_msg_id` alone: a plain reply, even to
  a message inside a topic, lands in the "All" view.

This module holds two things:

- reply detection (`resolve_reply_target`, `is_real_reply`): whether a
  message really replies to another one, given that every topic message has
  a reply header;
- send placement (`TopicPlacement`, `TopicPlacementMixin`): a reply to a
  message in a private topic gets that topic's `top_msg_id`, on every send
  path at once.

It imports only the standard library and Telethon, so tests and tools can
load it without ``uniborg.util``.
"""
import copy
from dataclasses import dataclass
import enum
import logging
from typing import Any, Awaitable, Callable, Dict, Optional, Tuple, Union

from telethon import errors
from telethon import utils as tl_utils
from telethon.tl import functions, types

#: How many topic roots `TOPIC_ROOTS` remembers, across all chats.
TOPIC_ROOT_CACHE_SIZE = 4096

#: How many private-chat messages a `TopicRegistry` remembers, across all
#: chats. Every private message the client sees or sends takes one entry.
TOPIC_REGISTRY_SIZE = 16384

#: The requests `TopicPlacement` places: every send Telethon gives a reply
#: target. `ForwardMessagesRequest` has `reply_to` and `top_msg_id` too, but
#: `forward_messages` sets neither, so a forward never carries a target to
#: place by.
PLACED_REQUEST_TYPES = (
    functions.messages.SendMessageRequest,
    functions.messages.SendMediaRequest,
    functions.messages.SendMultiMediaRequest,
)

_log = logging.getLogger(__name__)

FetchMessage = Callable[[Any, int], Awaitable[Any]]


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


class _Unknown(enum.Enum):
    UNKNOWN = "unknown"

    def __repr__(self) -> str:
        return "UNKNOWN"


#: What `TopicRegistry.get` returns for a message it has not seen. None is a
#: different answer: the message is known to be outside every topic.
UNKNOWN = _Unknown.UNKNOWN

TopicLookup = Union[int, None, _Unknown]


class TopicRegistry(_BoundedMap):
    """The private topic of each message, keyed by chat and message id."""

    def __init__(self, *, max_size: int = TOPIC_REGISTRY_SIZE):
        super().__init__(max_size=max_size)

    def get(self, chat_id, msg_id: int) -> TopicLookup:
        return self._load((chat_id, msg_id), UNKNOWN)

    def record(self, chat_id, msg_id: int, *, topic_id: Optional[int]) -> None:
        self._store((chat_id, msg_id), topic_id)


@dataclass(frozen=True)
class ReplyTarget:
    """The message another message really replies to."""

    msg_id: int
    #: The replied-to message, when it was already at hand or resolving loaded
    #: it, so callers need not load it again.
    message: Optional[Any] = None


def is_topic_root(message) -> bool:
    return isinstance(getattr(message, "action", None), types.MessageActionTopicCreate)


def private_chat_id(peer) -> Optional[int]:
    """The user's id when PEER is a private chat with a user, else None.

    PEER may be anything `telethon.utils.get_peer` accepts: a marked id, a
    Peer, an InputPeer or an entity. A username, the self peer and None give
    None, since they do not show what kind of chat they are.
    """
    if peer is None:
        return None
    try:
        peer = tl_utils.get_peer(peer)
    except (TypeError, ValueError):
        return None
    if isinstance(peer, types.PeerUser):
        return peer.user_id
    return None


def private_topic_id(message) -> Optional[int]:
    """The private topic MESSAGE sits in, read from its reply header.

    Only meaningful in a private chat: a forum supergroup leaves
    `reply_to_top_id` unset on a message that replies to nothing.
    """
    header = getattr(message, "reply_to", None)
    if isinstance(header, types.MessageReplyHeader) and header.forum_topic:
        return header.reply_to_top_id
    return None


def private_message_topic_id(message) -> Optional[int]:
    """The private topic MESSAGE sits in, or None outside topics.

    Unlike `private_topic_id`, this checks the chat first, so it is safe on a
    message from any chat: outside private chats with users it gives None. It
    also reads the message Telethon builds itself for an
    `UpdateShortSentMessage`, whose `reply_to` is the request's
    `InputReplyToMessage` rather than a header; there `top_msg_id` is the
    topic the send was placed in.
    """
    if private_chat_id(getattr(message, "peer_id", None)) is None:
        return None
    reply_to = getattr(message, "reply_to", None)
    if isinstance(reply_to, types.InputReplyToMessage):
        return reply_to.top_msg_id
    return private_topic_id(message)


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
      `TopicPlacement` also files each root it sees arrive there.

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


@dataclass(frozen=True)
class Placement:
    """Where one private-chat send lands: in TOPIC_ID, or in "All" for None."""

    chat_id: int
    topic_id: Optional[int]
    #: The reply target before placement replaced it; None when placement
    #: left the request alone.
    unplaced_reply_to: Optional[Any] = None


@dataclass
class TopicPlacementStats:
    """What a `TopicPlacement` has done, for logs and tests."""

    #: Sends that were given a `top_msg_id`.
    placed: int = 0
    #: Replied-to messages loaded because the registry had not seen them.
    fetched: int = 0
    #: Placed sends Telegram refused over their topic, then sent unplaced.
    refused: int = 0
    #: Lookups or bookkeeping that raised; those sends went unchanged.
    failures: int = 0


def is_topic_refusal(error) -> bool:
    """Whether ERROR is Telegram refusing a send over its topic.

    `TOPIC_DELETED` has its own class; other ``TOPIC_*`` errors (such as a
    closed topic) arrive as a generic `RPCError` carrying the name.
    """
    if isinstance(error, getattr(errors, "TopicDeletedError", ())):
        return True
    message = getattr(error, "message", None) or ""
    return isinstance(error, errors.RPCError) and message.upper().startswith("TOPIC_")


def _result_updates(result) -> list:
    if isinstance(result, types.UpdateShort):
        return [result.update]
    if isinstance(result, (types.Updates, types.UpdatesCombined)):
        return list(result.updates)
    return []


class TopicPlacement:
    """Places replies in private topics, and remembers where messages sit.

    - `place` gives a send that replies to a message in a private topic that
      topic's `top_msg_id`. It looks the replied-to message up in REGISTRY;
      on a miss it loads that message once, by id, with the FETCH_MESSAGE it
      is handed (bots may load messages by id, not history).
    - `record_update` files incoming private messages in REGISTRY, and topic
      roots in TOPIC_ROOTS (reply detection's cache).
    - `record_sent` files the messages a send produced under the topic they
      landed in, so replies to the client's own messages, such as the chunks
      of a split answer, stay in the topic too.

    Only private chats with users are touched: a forum supergroup already
    places a reply by its `reply_to`. A send with no reply target is never
    placed, since nothing says which topic it belongs to; it lands in "All".
    Nothing here raises into a send: `before_send`, `record_update`,
    `record_sent` and `unplace_refused` log the first problem, count every
    one, and carry on. A placed send that Telegram refuses over its topic
    (`is_topic_refusal`) goes out again unplaced, so placement never loses a
    message that would have been sent without it.
    """

    def __init__(
        self,
        *,
        registry: Optional[TopicRegistry] = None,
        topic_roots: Optional[TopicRootCache] = None,
        request_types: Tuple[type, ...] = PLACED_REQUEST_TYPES,
        logger: Optional[logging.Logger] = None,
    ):
        self.registry = registry if registry is not None else TopicRegistry()
        #: None follows the module's TOPIC_ROOTS, as `resolve_reply_target` does.
        self._topic_roots = topic_roots
        self.request_types = tuple(request_types)
        self.stats = TopicPlacementStats()
        self._log = logger or _log
        self._problem_logged = False

    @property
    def topic_roots(self) -> TopicRootCache:
        return TOPIC_ROOTS if self._topic_roots is None else self._topic_roots

    def handles(self, request) -> bool:
        return isinstance(request, self.request_types)

    def _file(
        self, chat_id: int, msg_id: int, *, topic_id: Optional[int], root: bool
    ) -> None:
        self.registry.record(chat_id, msg_id, topic_id=topic_id)
        if root and topic_id is not None:
            self.topic_roots.remember(chat_id, topic_id, root_id=msg_id)

    def record_message(self, message) -> bool:
        """Files MESSAGE under its topic if it is a private-chat message.

        Returns whether it was filed. May raise on a malformed object; the
        `record_*` entry points catch that.
        """
        chat_id = private_chat_id(getattr(message, "peer_id", None))
        msg_id = getattr(message, "id", None)
        if chat_id is None or not msg_id:
            return False
        self._file(
            chat_id,
            msg_id,
            topic_id=private_topic_id(message),
            root=is_topic_root(message),
        )
        return True

    def record_update(self, update) -> None:
        """Files the private-chat message UPDATE carries, if any."""
        try:
            if isinstance(update, (types.UpdateNewMessage, types.UpdateEditMessage)):
                self.record_message(update.message)
            elif isinstance(update, types.UpdateShortMessage):
                self._file(
                    update.user_id,
                    update.id,
                    topic_id=private_topic_id(update),
                    root=False,
                )
        except Exception:
            self._note_failure("filing an incoming message")

    async def topic_of(
        self, chat_id: int, msg_id: int, *, peer, fetch_message: FetchMessage
    ) -> Optional[int]:
        """The private topic of message MSG_ID in CHAT_ID, loading it on a miss.

        PEER is the chat as the send names it, handed to FETCH_MESSAGE. A
        message that no longer exists counts as outside every topic.
        """
        known = self.registry.get(chat_id, msg_id)
        if known is not UNKNOWN:
            return known
        self.stats.fetched += 1
        message = await fetch_message(peer, msg_id)
        topic_id = private_topic_id(message)
        self._file(chat_id, msg_id, topic_id=topic_id, root=is_topic_root(message))
        return topic_id

    async def place(
        self, request, *, fetch_message: FetchMessage
    ) -> Optional[Placement]:
        """Sets REQUEST's `top_msg_id` when it replies to a private-topic message.

        Returns where the send lands, or None when that is unknown or REQUEST
        is no private-chat send. The lookup may raise; `before_send` is the
        entry point that never does. REQUEST's reply target is replaced by a
        copy, never edited in place, and only once the lookup has succeeded.
        """
        if not self.handles(request):
            return None
        chat_id = private_chat_id(request.peer)
        if chat_id is None:
            return None
        reply_to = request.reply_to
        if reply_to is None:
            return Placement(chat_id=chat_id, topic_id=None)
        if not isinstance(reply_to, types.InputReplyToMessage):
            return None
        if reply_to.reply_to_peer_id is not None:
            #: The replied-to message lives in another chat, whose ids and
            #: topics say nothing about this one.
            return None
        if reply_to.top_msg_id is not None:
            return Placement(chat_id=chat_id, topic_id=reply_to.top_msg_id)
        if not reply_to.reply_to_msg_id:
            return None

        topic_id = await self.topic_of(
            chat_id,
            reply_to.reply_to_msg_id,
            peer=request.peer,
            fetch_message=fetch_message,
        )
        if topic_id is None:
            return Placement(chat_id=chat_id, topic_id=None)
        placed = copy.copy(reply_to)
        placed.top_msg_id = topic_id
        request.reply_to = placed
        self.stats.placed += 1
        return Placement(chat_id=chat_id, topic_id=topic_id, unplaced_reply_to=reply_to)

    async def before_send(
        self, request, *, fetch_message: FetchMessage
    ) -> Optional[Placement]:
        """`place`, except that a failure leaves REQUEST unchanged and gives None."""
        try:
            return await self.place(request, fetch_message=fetch_message)
        except Exception:
            self._note_failure("placing a send in its topic")
            return None

    def unplace_refused(
        self, request, *, error: Exception, placement: Optional[Placement]
    ) -> Optional[Placement]:
        """Undoes PLACEMENT on REQUEST when ERROR refused it over its topic.

        Returns the placement to retry the send under, or None when the send
        should fail with ERROR as it would have without placement. The parent
        is filed as outside topics, so later replies to it are not placed
        again.
        """
        try:
            if (
                placement is None
                or placement.unplaced_reply_to is None
                or not is_topic_refusal(error)
            ):
                return None
            original = placement.unplaced_reply_to
            request.reply_to = original
            self.registry.record(
                placement.chat_id, original.reply_to_msg_id, topic_id=None
            )
            self.stats.refused += 1
            self._note_problem(
                f"Telegram refused a send in topic {placement.topic_id} ({error}); "
                "sending it unplaced",
                exc_info=False,
            )
            return Placement(chat_id=placement.chat_id, topic_id=None)
        except Exception:
            self._note_failure("undoing a refused placement")
            return None

    def record_sent(self, request, result, *, placement: Optional[Placement]) -> None:
        """Files the messages RESULT reports for the send REQUEST.

        A message echoed back in full is filed by its own reply header, which
        is Telegram's word on where it landed. A bare id (`UpdateMessageID`,
        `UpdateShortSentMessage`) is filed under PLACEMENT, when there is one.
        """
        try:
            self._record_sent(request, result, placement=placement)
        except Exception:
            self._note_failure("filing a sent message")

    def _record_sent(self, request, result, *, placement: Optional[Placement]):
        if getattr(request, "schedule_date", None) is not None:
            #: Scheduled messages have ids of their own, which can collide
            #: with ordinary ones.
            return
        if isinstance(result, types.UpdateShortSentMessage):
            if placement is not None:
                self._file(
                    placement.chat_id,
                    result.id,
                    topic_id=placement.topic_id,
                    root=False,
                )
            return

        updates = _result_updates(result)
        echoed = set()
        for update in updates:
            if isinstance(update, types.UpdateNewMessage) and self.record_message(
                update.message
            ):
                echoed.add(update.message.id)
        if placement is None:
            return
        for update in updates:
            if isinstance(update, types.UpdateMessageID) and update.id not in echoed:
                self._file(
                    placement.chat_id,
                    update.id,
                    topic_id=placement.topic_id,
                    root=False,
                )

    def _note_failure(self, what: str) -> None:
        self.stats.failures += 1
        self._note_problem(
            f"Topic placement failed while {what}; sends it affects go out unplaced",
            exc_info=True,
        )

    def _note_problem(self, text: str, *, exc_info: bool) -> None:
        """Logs TEXT as a warning the first time, at DEBUG afterwards."""
        if self._problem_logged:
            self._log.debug("%s", text, exc_info=exc_info)
            return
        self._problem_logged = True
        self._log.warning(
            "%s. Later problems are only counted (see `stats`) and logged at DEBUG.",
            text,
            exc_info=exc_info,
        )


class TopicPlacementMixin:
    """Places a client's replies in private topics (see `TopicPlacement`).

    Put it first in the bases, before `telethon_safety.DifferenceFallbackMixin`
    and `TelegramClient`, so both `__call__` overrides run, this one
    outermost. Every Telethon send ends in ``await self(request)``, so this
    `__call__` sees each one, whichever helper or plugin made it. Without a
    `topic_placement` attached it changes nothing.
    """

    #: None keeps Telethon's behaviour; `Uniborg.create` attaches one.
    topic_placement: Optional[TopicPlacement] = None

    #: The signature matches `TelegramClient.__call__`, which callers may use
    #: positionally.
    async def __call__(self, request, ordered=False, flood_sleep_threshold=None):
        call = super().__call__
        engine = self.topic_placement
        if engine is None or not engine.handles(request):
            return await call(
                request, ordered=ordered, flood_sleep_threshold=flood_sleep_threshold
            )

        placement = await engine.before_send(
            request, fetch_message=self._fetch_topic_message
        )
        try:
            result = await call(
                request, ordered=ordered, flood_sleep_threshold=flood_sleep_threshold
            )
        except errors.RPCError as error:
            placement = engine.unplace_refused(
                request, error=error, placement=placement
            )
            if placement is None:
                raise
            result = await call(
                request, ordered=ordered, flood_sleep_threshold=flood_sleep_threshold
            )
        engine.record_sent(request, result, placement=placement)
        return result

    async def _fetch_topic_message(self, peer, msg_id: int):
        return await self.get_messages(peer, ids=msg_id)

    #: Telethon's update loop hands every update to ``self._dispatch_update``
    #: (the same private method in 1.43.2 and 1.45.0). Filing here, before
    #: any handler runs, lets a handler's reply find its parent without a
    #: fetch. An event handler cannot do that: Uniborg runs the newest
    #: handler first, so one registered at startup would run after every
    #: plugin's.
    async def _dispatch_update(self, update):
        engine = self.topic_placement
        if engine is not None:
            engine.record_update(update)
        return await super()._dispatch_update(update)
