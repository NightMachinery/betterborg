"""Automatic titles for new private topics.

A message typed in a bot's "All" view makes Telegram open a topic for it,
named after the message and flagged `title_missing`. Once the bot has
answered the first message of such a topic, it renames the topic: the
answering model's emoji and reasoning-effort alias, then a short title from
the user's title model. Each topic is renamed at most once. See
docs/topic_titles.md.

The topic id here is T, the id Telegram puts in `reply_to_top_id`
(docs/private_topics.md); `messages.editForumTopic` refuses the root's id.
"""

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Awaitable, Callable, Optional

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
class TopicTitleRequest:
    """One answered message in a private topic, as the rename needs it."""

    #: The private chat, as Telethon accepts a peer.
    peer: Any
    chat_id: int
    #: T, from the message's `reply_to_top_id`.
    topic_id: int
    message_date: datetime
    question: str
    answer: str
    #: The answering model's emoji and its reasoning effort's alias ("" when
    #: the model has no reasoning levels).
    model_emoji: str
    effort_alias: str


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


def topic_title_prompt(question: str, answer: str) -> str:
    return TOPIC_TITLE_PROMPT.format(
        max_words=TOPIC_TITLE_MAX_WORDS,
        question=question[:EXCHANGE_SIDE_CHARS].strip() or "(no text)",
        answer=answer[:EXCHANGE_SIDE_CHARS].strip() or "(no text)",
    )


def compose_topic_title(
    title: str, *, model_emoji: str, effort_alias: str
) -> Optional[str]:
    """`<emoji><alias> <title>`, within Telegram's limit; None if TITLE is blank."""
    title = " ".join(title.split()).strip(" \"'“”«».")
    if not title:
        return None
    prefix = f"{model_emoji}{effort_alias}"
    full = f"{prefix} {title}" if prefix else title
    return tg_format.truncate_utf16(full, TOPIC_TITLE_MAX_UNITS)


async def title_new_topic(
    client,
    request: TopicTitleRequest,
    *,
    generate: TopicTitleGenerator,
    marks: Optional[TopicTitleMarks] = None,
) -> Optional[str]:
    """Rename REQUEST's topic if this was its first answer; return the title.

    GENERATE turns a prompt into a `TopicTitle`. Returns None when the topic
    is not renamed: it was claimed before, the user named it, it is gone, or
    the answered message is not one of its first.
    """
    marks = marks or MARKS
    if not await marks.claim(request.chat_id, request.topic_id):
        return None
    topic = await fetch_forum_topic(client, request.peer, request.topic_id)
    if topic is None or not is_first_message_of_untitled_topic(
        topic, message_date=request.message_date
    ):
        return None
    generated = await generate(topic_title_prompt(request.question, request.answer))
    title = compose_topic_title(
        generated.title,
        model_emoji=request.model_emoji,
        effort_alias=request.effort_alias,
    )
    if title is None:
        return None
    await client(
        functions.messages.EditForumTopicRequest(
            peer=request.peer, topic_id=request.topic_id, title=title
        )
    )
    return title


_background_tasks: set = set()


def schedule_title_new_topic(
    client,
    request: TopicTitleRequest,
    *,
    generate: TopicTitleGenerator,
    marks: Optional[TopicTitleMarks] = None,
) -> asyncio.Task:
    """Run `title_new_topic` in the background, logging its failures.

    A rename must not hold up the answer: Telethon sleeps through a flood
    wait on the caller's time, and the title model may be slow.
    """

    async def run():
        try:
            return await title_new_topic(
                client, request, generate=generate, marks=marks
            )
        except Exception:
            logger.exception(
                "Could not title topic %s of chat %s",
                request.topic_id,
                request.chat_id,
            )
            return None

    task = asyncio.create_task(run())
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    return task
