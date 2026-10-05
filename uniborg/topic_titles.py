"""Automatic titles for new private topics.

A message typed in a bot's "All" view makes Telegram open a topic for it,
named implicitly and flagged `title_missing`. When the bot starts
answering the first message of such a topic, it renames the topic at once to
the model's badge (its emoji and reasoning-effort symbol) and Telegram's
name (New Chat or question text), and sets the model's topic icon. Once the
answer is delivered, it renames the topic again, to the badge and a short
title from the user's title model, optionally chooses a matching icon, and
immediately deletes each
rename's service message for both participants. Each topic is claimed
at most once. See docs/topic_titles.md.

The topic id here is T, the id Telegram puts in `reply_to_top_id`
(docs/private_topics.md); `messages.editForumTopic` refuses the root's id.
"""

import asyncio
import logging
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Any, Awaitable, Callable, Dict, Optional

from pydantic import BaseModel, Field
from telethon.tl import functions, types

from uniborg import redis_util, tg_format

#: Telegram's limit for a topic title (the Bot API says 1 to 128 characters).
TOPIC_TITLE_MAX_UNITS = 128
TOPIC_TITLE_MAX_WORDS = 6
#: The answered message counts as its topic's first when it was sent this soon
#: after the topic was opened. A topic typed in "All" opens with its message,
#: so the window only has to cover a retry after a failed first answer.
NEW_TOPIC_WINDOW = timedelta(minutes=10)
#: Characters of the question, and of the answer, that the title model sees.
EXCHANGE_SIDE_CHARS = 3000
#: Claims kept in memory when Redis is unreachable. Forgetting an old claim
#: costs one `getForumTopicsByID`: its topic is past the window by then.
MEMORY_MARKS_MAX = 4096
INITIAL_NAME_STYLES = {"new_chat": "New Chat", "question": "Question text"}
DEFAULT_INITIAL_NAME_STYLE = "new_chat"

TOPIC_TITLE_PROMPT = """Write a title for a chat topic that starts with the exchange below.

- At most {max_words} words, in the language of the user's message.
- Name the subject. No quotes, no emoji, no trailing period.

<user>
{question}
</user>

<assistant>
{answer}
</assistant>"""

logger = logging.getLogger(__name__)


class TopicTitle(BaseModel):
    title: str = Field(
        description=f"The topic's title, at most {TOPIC_TITLE_MAX_WORDS} words."
    )
    icon_emoji: str = Field(
        default="", description="One topic icon emoji from the supplied list."
    )


TopicTitleGenerator = Callable[[str], Awaitable[TopicTitle]]


class TopicTitleMarks:
    """Claims each (chat, topic) for its automatic title, once.

    Claims live in Redis when it is reachable, so a restart does not title a
    topic twice, and in this process otherwise (the newest
    `MEMORY_MARKS_MAX`).
    """

    def __init__(
        self,
        *,
        ttl_seconds: Optional[int] = None,
        get_redis: Optional[Callable[[], Awaitable[Any]]] = None,
    ):
        self._ttl_seconds = ttl_seconds
        self._get_redis = get_redis or redis_util.get_redis
        #: Insertion-ordered, oldest first.
        self._memory: dict = {}

    @property
    def ttl_seconds(self) -> int:
        return self._ttl_seconds or redis_util.get_long_expire_duration()

    @staticmethod
    def key(chat_id: int, topic_id: int) -> str:
        return f"borg:topic_titled:{chat_id}:{topic_id}"

    async def claim(self, chat_id: int, topic_id: int) -> bool:
        """True for the first claim of (CHAT_ID, TOPIC_ID), False after."""
        redis_client = await self._get_redis()
        if redis_client is not None:
            try:
                return bool(
                    await redis_client.set(
                        self.key(chat_id, topic_id),
                        "1",
                        nx=True,
                        ex=self.ttl_seconds,
                    )
                )
            except Exception as e:
                redis_util.note_error(e)
                logger.warning("Topic title marks: Redis failed, using memory: %s", e)
        return self._claim_in_memory((chat_id, topic_id))

    def _claim_in_memory(self, key) -> bool:
        if key in self._memory:
            return False
        self._memory[key] = None
        if len(self._memory) > MEMORY_MARKS_MAX:
            del self._memory[next(iter(self._memory))]
        return True


MARKS = TopicTitleMarks()


@dataclass(frozen=True)
class TopicBadge:
    """What a renamed topic shows of the model answering in it."""

    model_emoji: str
    #: The reasoning effort's symbol, "" when the model has no levels.
    effort_symbol: str
    #: The emoji of the topic icon to set, one of Telegram's default topic
    #: icons (`TopicIcons`); "" leaves the icon alone.
    icon_emoji: str = ""

    @property
    def prefix(self) -> str:
        return f"{self.model_emoji}{self.effort_symbol}"


@dataclass(frozen=True)
class TopicRef:
    """A private topic, and when the message being answered in it was sent."""

    #: The private chat, as Telethon accepts a peer.
    peer: Any
    chat_id: int
    #: T, from the message's `reply_to_top_id`.
    topic_id: int
    message_date: datetime


@dataclass(frozen=True)
class TopicTitleRequest:
    """One answered message in a private topic, as the title rename needs it."""

    topic: TopicRef
    question: str
    answer: str
    badge: TopicBadge


@dataclass(frozen=True)
class PrefixedTopic:
    """A new topic, claimed and renamed to its badge and Telegram's name."""

    #: A service message whose immediate deletion failed, retried after the
    #: title's rename. None when no cleanup remains.
    service_message_id: Optional[int]


class TopicIcons:
    """Telegram's default topic icons by emoji: the only custom emoji a bot,
    which cannot have Premium, may set as a topic icon.

    The set is loaded once, on first use. A failed load is logged and retried
    on the next use; meanwhile topics keep their icons.
    """

    def __init__(self):
        self._by_emoji: Optional[Dict[str, int]] = None

    @staticmethod
    def _plain(emoji: str) -> str:
        return emoji.replace("\ufe0f", "")

    async def _load(self, client) -> None:
        if self._by_emoji is None:
            try:
                icons = await client(
                    functions.messages.GetStickerSetRequest(
                        stickerset=types.InputStickerSetEmojiDefaultTopicIcons(),
                        hash=0,
                    )
                )
            except Exception:
                logger.exception("Could not load the default topic icons")
                return
            self._by_emoji = {
                self._plain(attribute.alt): document.id
                for document in icons.documents
                for attribute in document.attributes
                if isinstance(attribute, types.DocumentAttributeCustomEmoji)
            }

    async def emojis(self, client) -> list[str]:
        """Supported emoji, for the title model to choose from."""
        await self._load(client)
        return list(self._by_emoji or {})

    async def document_id(self, client, emoji: str) -> Optional[int]:
        """The custom emoji id of the default topic icon EMOJI, or None."""
        if not emoji:
            return None
        await self._load(client)
        return (self._by_emoji or {}).get(self._plain(emoji))


ICONS = TopicIcons()


async def fetch_forum_topic(client, peer, topic_id: int) -> Optional[types.ForumTopic]:
    """The topic TOPIC_ID of the private chat PEER, or None if it is gone."""
    result = await client(
        functions.messages.GetForumTopicsByIDRequest(peer=peer, topics=[topic_id])
    )
    for topic in result.topics:
        if isinstance(topic, types.ForumTopic) and topic.id == topic_id:
            return topic
    return None


def is_first_message_of_untitled_topic(
    topic: types.ForumTopic,
    *,
    message_date: datetime,
    window: timedelta = NEW_TOPIC_WINDOW,
) -> bool:
    """Whether TOPIC was named by Telegram, not by the user, and a message
    sent at MESSAGE_DATE is one of its first.

    `title_missing` stays set after a bot renames a topic, so it says how the
    topic was opened, not whether it has been renamed since.
    """
    if not topic.title_missing or topic.date is None:
        return False
    return message_date - topic.date <= window


def topic_title_prompt(
    question: str, answer: str, *, icon_emojis: Optional[list[str]] = None
) -> str:
    prompt = TOPIC_TITLE_PROMPT.format(
        max_words=TOPIC_TITLE_MAX_WORDS,
        question=question[:EXCHANGE_SIDE_CHARS].strip() or "(no text)",
        answer=answer[:EXCHANGE_SIDE_CHARS].strip() or "(no text)",
    )
    if icon_emojis:
        prompt += (
            "\n\nChoose icon_emoji to match the subject, from these topic icons: "
            + " ".join(icon_emojis)
            + ". Keep the title itself free of emoji."
        )
    return prompt


def initial_topic_title(question: str, *, style: str) -> str:
    """The initial name while the first answer is being generated."""
    if style == "new_chat":
        return "New Chat"
    elif style == "question":
        return " ".join(question.split()) or "New Chat"
    else:
        raise ValueError(f"Unknown initial topic name style: {style!r}")


def _with_prefix(title: str, *, prefix: str) -> str:
    full = f"{prefix} {title}" if prefix else title
    return tg_format.truncate_utf16(full, TOPIC_TITLE_MAX_UNITS)


def compose_topic_title(title: str, *, badge: TopicBadge) -> Optional[str]:
    """`<emoji><symbol> <title>`, within Telegram's limit; None if TITLE is blank."""
    title = " ".join(title.split()).strip(" \"'“”«».")
    if not title:
        return None
    return _with_prefix(title, prefix=badge.prefix)


def prefixed_topic_title(telegram_title: str, *, badge: TopicBadge) -> str:
    """BADGE's prefix before the name Telegram gave a topic, kept as it is."""
    return _with_prefix(telegram_title, prefix=badge.prefix)


def _service_message_id(updates) -> Optional[int]:
    for update in getattr(updates, "updates", None) or []:
        message = getattr(update, "message", None)
        if isinstance(message, types.MessageService) and isinstance(
            message.action, types.MessageActionTopicEdit
        ):
            return message.id
    return None


async def _rename(
    client,
    topic: TopicRef,
    *,
    title: str,
    badge: TopicBadge,
    icons: TopicIcons,
) -> Optional[int]:
    """Rename TOPIC with BADGE's icon, then remove its service message.

    Return the service message id only when its deletion failed.
    """
    updates = await client(
        functions.messages.EditForumTopicRequest(
            peer=topic.peer,
            topic_id=topic.topic_id,
            title=title,
            icon_emoji_id=await icons.document_id(client, badge.icon_emoji),
        )
    )
    message_id = _service_message_id(updates)
    if message_id is not None and not await _delete_service_message(client, message_id):
        return message_id
    return None


async def _claim_new_topic(
    client, topic: TopicRef, *, marks: TopicTitleMarks
) -> Optional[types.ForumTopic]:
    """TOPIC, if this is its first claim and the answered message is one of
    the first of a topic Telegram named; None otherwise."""
    if not await marks.claim(topic.chat_id, topic.topic_id):
        return None
    forum_topic = await fetch_forum_topic(client, topic.peer, topic.topic_id)
    if forum_topic is None or not is_first_message_of_untitled_topic(
        forum_topic, message_date=topic.message_date
    ):
        return None
    return forum_topic


async def prefix_new_topic(
    client,
    topic: TopicRef,
    *,
    badge: TopicBadge,
    marks: Optional[TopicTitleMarks] = None,
    icons: Optional[TopicIcons] = None,
    initial_title: Optional[str] = None,
) -> Optional[PrefixedTopic]:
    """Claim TOPIC if it is new, and rename it at once to BADGE's prefix and
    the name Telegram gave it, with BADGE's icon.

    Returns None when the topic is not new (see `title_new_topic`). A failed
    rename is logged, and the topic stays claimed for its title.
    """
    forum_topic = await _claim_new_topic(client, topic, marks=marks or MARKS)
    if forum_topic is None:
        return None
    try:
        service_message_id = await _rename(
            client,
            topic,
            title=prefixed_topic_title(
                forum_topic.title if initial_title is None else initial_title,
                badge=badge,
            ),
            badge=badge,
            icons=icons or ICONS,
        )
    except Exception:
        logger.exception(
            "Could not prefix topic %s of chat %s", topic.topic_id, topic.chat_id
        )
        service_message_id = None
    return PrefixedTopic(service_message_id=service_message_id)


async def _delete_service_message(client, message_id: int) -> bool:
    try:
        await client(
            functions.messages.DeleteMessagesRequest(id=[message_id], revoke=True)
        )
        return True
    except Exception:
        logger.exception("Could not delete the topic rename message %s", message_id)
        return False


async def title_new_topic(
    client,
    request: TopicTitleRequest,
    *,
    generate: TopicTitleGenerator,
    prefixed: Optional[Awaitable[Optional[PrefixedTopic]]] = None,
    marks: Optional[TopicTitleMarks] = None,
    icons: Optional[TopicIcons] = None,
    choose_icon: bool = False,
) -> Optional[str]:
    """Rename REQUEST's topic to its title if this was its first answer;
    return the title.

    PREFIXED is the outcome of `prefix_new_topic` for this answer, when it ran:
    the topic was claimed there. Any failed cleanup of the prefix rename's
    service message is retried once the title is set. Without it the topic
    is claimed here. Each rename's service message is deleted immediately.
    GENERATE turns a prompt into a `TopicTitle`. Returns None when the topic
    is not renamed: it was claimed before, the user named it, it is gone, or
    the answered message is not one of its first.
    """
    if prefixed is None:
        if await _claim_new_topic(client, request.topic, marks=marks or MARKS) is None:
            return None
        prefix_message_id = None
    else:
        claimed = await prefixed
        if claimed is None:
            return None
        prefix_message_id = claimed.service_message_id
    icons = icons or ICONS
    icon_emojis = await icons.emojis(client) if choose_icon else []
    generated = await generate(
        topic_title_prompt(request.question, request.answer, icon_emojis=icon_emojis)
    )
    title = compose_topic_title(generated.title, badge=request.badge)
    if title is None:
        return None
    badge = request.badge
    if choose_icon and TopicIcons._plain(generated.icon_emoji) in icon_emojis:
        badge = replace(badge, icon_emoji=generated.icon_emoji)
    await _rename(client, request.topic, title=title, badge=badge, icons=icons)
    if prefix_message_id is not None:
        await _delete_service_message(client, prefix_message_id)
    return title


_background_tasks: set = set()


def _in_background(make, *, what: str, topic: TopicRef) -> asyncio.Task:
    """Run the coroutine MAKE returns in the background, logging failures.

    A rename must not hold up the answer: Telethon sleeps through a flood
    wait on the caller's time, and the title model may be slow.
    """

    async def run():
        try:
            return await make()
        except Exception:
            logger.exception(
                "Could not %s topic %s of chat %s",
                what,
                topic.topic_id,
                topic.chat_id,
            )
            return None

    task = asyncio.create_task(run())
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    return task


def schedule_prefix_new_topic(
    client,
    topic: TopicRef,
    *,
    badge: TopicBadge,
    marks: Optional[TopicTitleMarks] = None,
    icons: Optional[TopicIcons] = None,
    initial_title: Optional[str] = None,
) -> asyncio.Task:
    """Run `prefix_new_topic` in the background; the task gives its result."""
    return _in_background(
        lambda: prefix_new_topic(
            client,
            topic,
            badge=badge,
            marks=marks,
            icons=icons,
            initial_title=initial_title,
        ),
        what="prefix",
        topic=topic,
    )


def schedule_title_new_topic(
    client,
    request: TopicTitleRequest,
    *,
    generate: TopicTitleGenerator,
    prefixed: Optional[Awaitable[Optional[PrefixedTopic]]] = None,
    marks: Optional[TopicTitleMarks] = None,
    icons: Optional[TopicIcons] = None,
    choose_icon: bool = False,
) -> asyncio.Task:
    """Run `title_new_topic` in the background."""
    return _in_background(
        lambda: title_new_topic(
            client,
            request,
            generate=generate,
            prefixed=prefixed,
            marks=marks,
            icons=icons,
            choose_icon=choose_icon,
        ),
        what="title",
        topic=request.topic,
    )
