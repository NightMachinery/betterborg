"""Streaming an answer as a Telegram draft, instead of by editing a message.

A bot can show a private chat a live draft (`messages.setTyping` with
`SendMessageTextDraftAction`) that the user's client turns into the real
message once it arrives. The rules this follows, and why, are in
docs/telegram_ai_apis.md, section 2.1; how the chat bot uses it is in
docs/draft_streaming.md.

`DraftAnswerMessage` stands in for the placeholder message that streaming
code edits. While the answer streams, edits update the draft, and replies
(the further chunks of a long answer) become parts of it; the draft shows the
last part. `end_stream` stops the drafts; after it, the first edit of a part
sends it as a real message, and `flush` sends whatever was never edited
again. A draft that Telegram refuses (`TEXTDRAFT_PEER_INVALID`: groups) makes
`start` return False, so the caller can stream by edits instead.
"""

import asyncio
from dataclasses import dataclass
import inspect
import itertools
import logging
import math
import secrets
import time
from typing import Any, Callable, Optional

from telethon import errors, functions, types

from uniborg import tg_format

_log = logging.getLogger(__name__)

_DRAFT_ACTION = getattr(types, "SendMessageTextDraftAction", None)
#: This Telethon can build a draft.
DRAFTS_SUPPORTED = _DRAFT_ACTION is not None
#: And a draft with a Stop button, whose press arrives as an update.
STOP_SUPPORTED = (
    DRAFTS_SUPPORTED
    and "can_stop" in inspect.signature(_DRAFT_ACTION.__init__).parameters
    and hasattr(types, "SendMessageStopDraftAction")
)

#: Telegram's limit for a draft's text, in UTF-16 code units.
DRAFT_LIMIT_UNITS = 4096
#: Edits of a draft-streamed answer are cheap, so streaming keeps this pace.
DRAFT_MIN_INTERVAL = 1.0
#: A draft vanishes 30 s after its last update; resend it well before that.
DRAFT_HEARTBEAT_SECONDS = 20.0
#: A draft call that takes longer is treated as a flood wait of this long.
#: Telethon ignores a per-call `flood_sleep_threshold`: it sleeps through a
#: flood wait of up to a minute, then sends the stale draft, which could land
#: after the answer as a ghost. Cancelling the call cancels that sleep.
DRAFT_CALL_TIMEOUT = 5.0

_PART_IDS = itertools.count(-1, -1)
#: What Telegram answers a draft outside a private chat.
PEER_REFUSED = "TEXTDRAFT_PEER_INVALID"
#: Chats where Telegram answered `PEER_REFUSED`; they stream by edits from
#: then on, until a restart.
_REFUSED_CHATS = set()
#: Live streams by their draft's random id, for the Stop button.
_ACTIVE = {}


@dataclass(frozen=True)
class StreamingPace:
    #: Seconds between edits of the streaming answer.
    interval: float
    #: Ends the partial text, to say the answer is still being written.
    cursor: str


def streaming_pace(
    response_message: Any, *, elapsed: float, edit_interval: float
) -> StreamingPace:
    """How often a streaming loop shows the answer, ELAPSED seconds in.

    Edits slow to one per 15 s after 30 s, and one per minute after two, with
    a sleepier cursor, to spare the edit budget. A draft keeps the pace.
    """
    if isinstance(response_message, _DraftPart):
        return StreamingPace(
            interval=min(edit_interval, DRAFT_MIN_INTERVAL), cursor="▌"
        )
    if elapsed > 120:
        return StreamingPace(interval=60, cursor="▌💤💤")
    if elapsed > 30:
        return StreamingPace(interval=15, cursor="▌💤")
    return StreamingPace(interval=edit_interval, cursor="▌")


class _DraftPart:
    """One message of a draft-streamed answer, which `stream` sends at the end.

    The fake `id` stays the same after the part is sent, since
    `util.edit_message` keys its chains by it; `message` is the real one.
    """

    out = True
    reply_to_msg_id = None

    def __init__(self, stream: "DraftAnswerMessage", *, parent: Optional["_DraftPart"]):
        self._stream = stream
        self._parent = parent
        self.id = next(_PART_IDS)
        self.chat_id = stream.chat_id
        self.text = ""
        self.parse_mode = None
        self.link_preview = False
        self.buttons = None
        #: The real message, once sent.
        self.message = None
        self.deleted = False

    @property
    def raw_text(self) -> str:
        return self.text

    async def edit(
        self,
        text: str,
        parse_mode: Any = None,
        link_preview: bool = False,
        buttons: Any = None,
        **_ignored,
    ) -> "_DraftPart":
        await self._stream._edit_part(
            self,
            text,
            parse_mode=parse_mode,
            link_preview=link_preview,
            buttons=buttons,
        )
        return self

    async def reply(self, text: str, parse_mode: Any = None, **kwargs) -> "_DraftPart":
        return await self._stream._reply_to_part(
            self, text, parse_mode=parse_mode, link_preview=kwargs.get("link_preview")
        )

    async def respond(self, *args, **kwargs) -> Any:
        return await self._stream.event.respond(*args, **kwargs)

    async def delete(self, *args, **kwargs) -> None:
        await self._stream._delete_part(self)

    async def get_chat(self) -> Any:
        return await self._stream.event.get_chat()


class DraftAnswerMessage(_DraftPart):
    """The placeholder of an answer streamed as a draft; see the module docstring.

    `event` is the message being answered: the answer replies to it, and in
    a private topic the draft shows in TOP_MSG_ID. `on_stop`, when set, is
    called when the user presses the draft's Stop button.
    """

    def __init__(
        self,
        client: Any,
        *,
        event: Any,
        top_msg_id: Optional[int] = None,
        min_interval: float = DRAFT_MIN_INTERVAL,
        heartbeat_seconds: float = DRAFT_HEARTBEAT_SECONDS,
        call_timeout: float = DRAFT_CALL_TIMEOUT,
        clock: Callable[[], float] = time.monotonic,
        logger: Optional[logging.Logger] = None,
    ):
        self.client = client
        self.event = event
        self.chat_id = event.chat_id
        super().__init__(self, parent=None)
        self.user_id = event.sender_id
        self.top_msg_id = top_msg_id
        self.random_id = secrets.randbits(63)
        self.on_stop = None
        self.streaming = False
        self.stopped = False
        self._parts = [self]
        self._peer = None
        self._min_interval = min_interval
        self._heartbeat_seconds = heartbeat_seconds
        self._call_timeout = call_timeout
        self._clock = clock
        #: Where the stream logs; the plugin's logger when it passed one.
        self.logger = logger or _log
        self._started_at = None
        self._last_draft_at = None
        self._blocked_until = 0.0
        self._changed = asyncio.Event()
        self._worker = None
        self._send_lock = asyncio.Lock()

    # --- Drafts ---

    def _shown(self) -> Optional[_DraftPart]:
        live = [part for part in self._parts if not part.deleted]
        return live[-1] if live else None

    async def _send_draft(
        self, text: str, parse_mode: Any, *, suffix: str = ""
    ) -> None:
        """One draft update. Raises what Telegram raises, flood waits included.

        A call slower than `call_timeout` is cancelled and raises a flood wait.
        """
        text, entities = await self.client._parse_message_text(
            tg_format.tail_utf16(text, DRAFT_LIMIT_UNITS - len(suffix)), parse_mode
        )
        kwargs = {"can_stop": True} if STOP_SUPPORTED else {}
        action = _DRAFT_ACTION(
            text=types.TextWithEntities(text=text + suffix, entities=entities or []),
            random_id=self.random_id,
            **kwargs,
        )
        request = functions.messages.SetTypingRequest(
            peer=self._peer, action=action, top_msg_id=self.top_msg_id
        )
        try:
            await asyncio.wait_for(self.client(request), self._call_timeout)
        except asyncio.TimeoutError:
            raise errors.FloodWaitError(
                request=request, capture=math.ceil(self._call_timeout)
            ) from None
        finally:
            self._last_draft_at = self._clock()

    async def start(self, text: str = "", *, parse_mode: Any = "md") -> bool:
        """Shows the first draft, TEXT ("" shows "Thinking…").

        False when the draft failed, a flood wait included, or Telegram
        refused one in this chat before; the caller should then send a
        placeholder and stream by edits.
        """
        if not DRAFTS_SUPPORTED or self.chat_id in _REFUSED_CHATS:
            return False
        self.text = text
        self.parse_mode = parse_mode
        try:
            self._peer = await self.event.get_input_chat()
            await self._send_draft(text, parse_mode)
        except Exception as e:
            if isinstance(e, errors.RPCError) and e.message == PEER_REFUSED:
                _REFUSED_CHATS.add(self.chat_id)
            self.logger.info(
                "No draft in chat %s (%r); streaming by edits", self.chat_id, e
            )
            return False
        self.streaming = True
        self._started_at = self._clock()
        _ACTIVE[self.random_id] = self
        self._worker = asyncio.ensure_future(self._run())
        return True

    async def _run(self) -> None:
        """Sends the shown part when it changes, at most every `min_interval`.

        When nothing changes for `heartbeat_seconds`, resends it with the
        elapsed time, so the draft does not expire.
        """
        while True:
            try:
                await asyncio.wait_for(self._changed.wait(), self._heartbeat_seconds)
                heartbeat = False
            except asyncio.TimeoutError:
                heartbeat = True
            self._changed.clear()
            now = self._clock()
            wait = max(
                self._blocked_until - now,
                (self._last_draft_at or 0) + self._min_interval - now,
                0,
            )
            if wait:
                await asyncio.sleep(wait)
            if self.stopped:
                continue
            shown = self._shown()
            if shown is None:
                continue
            suffix = ""
            if heartbeat:
                elapsed = int(self._clock() - self._started_at)
                suffix = f"{' ' if shown.text else ''}⏳ {elapsed}s"
            try:
                await self._send_draft(shown.text, shown.parse_mode, suffix=suffix)
            except errors.FloodWaitError as e:
                self._blocked_until = self._clock() + e.seconds
            except Exception:
                self.logger.warning("Could not update a draft", exc_info=True)

    def stop_pressed(self) -> None:
        """The user pressed Stop: no more drafts, and `on_stop` is called."""
        if self.stopped:
            return
        self.stopped = True
        if self.on_stop is not None:
            self.on_stop()

    async def end_stream(self) -> None:
        """Stops the drafts. From now on, an edit sends its part for real."""
        if not self.streaming:
            return
        self.streaming = False
        _ACTIVE.pop(self.random_id, None)
        worker, self._worker = self._worker, None
        if worker is not None:
            worker.cancel()
            try:
                await worker
            except asyncio.CancelledError:
                pass

    async def flush(self) -> None:
        """Ends the stream and sends every part not yet sent, in order."""
        await self.end_stream()
        for part in self._parts:
            await self._send_part(part)

    # --- The parts ---

    def _changed_part(self) -> None:
        if self.streaming:
            self._changed.set()

    async def _edit_part(
        self, part: _DraftPart, text: str, *, parse_mode, link_preview, buttons
    ) -> None:
        if self.streaming and buttons is not None:
            #: A draft has no buttons.
            await self.end_stream()
        if part.message is not None:
            part.message = (
                await part.message.edit(
                    text,
                    parse_mode=parse_mode,
                    link_preview=link_preview,
                    buttons=buttons,
                )
                or part.message
            )
        part.text = text
        part.parse_mode = parse_mode
        part.link_preview = link_preview
        part.buttons = buttons
        part.deleted = False
        if self.streaming:
            self._changed_part()
        else:
            await self._send_part(part)

    async def _reply_to_part(
        self, parent: _DraftPart, text: str, *, parse_mode, link_preview
    ) -> _DraftPart:
        child = _DraftPart(self, parent=parent)
        child.text = text
        child.parse_mode = parse_mode
        child.link_preview = bool(link_preview)
        self._parts.append(child)
        if self.streaming:
            self._changed_part()
        else:
            await self._send_part(child)
        return child

    async def _delete_part(self, part: _DraftPart) -> None:
        part.deleted = True
        if part.message is not None:
            message, part.message = part.message, None
            await message.delete()
        if part is self:
            await self.end_stream()
        self._changed_part()

    def _sent_parent(self, part: _DraftPart) -> Optional[_DraftPart]:
        parent = part._parent
        while parent is not None and parent.deleted:
            parent = parent._parent
        return parent

    async def _send_part(self, part: _DraftPart) -> None:
        """Sends PART for real, after the parts before it; empty parts are not."""
        async with self._send_lock:
            await self._send_part_locked(part)

    async def _send_part_locked(self, part: _DraftPart) -> None:
        if part.message is not None or part.deleted or not part.text:
            return
        parent = self._sent_parent(part)
        if parent is not None:
            await self._send_part_locked(parent)
        if parent is not None and parent.message is not None:
            part.message = await parent.message.reply(
                part.text,
                parse_mode=part.parse_mode,
                link_preview=part.link_preview,
                buttons=part.buttons,
            )
            return
        if (
            not self.stopped
            and self._last_draft_at is not None
            and self._clock() >= self._blocked_until
        ):
            #: Telegram Desktop adopts the draft into the answer only when the
            #: last draft shares its start; otherwise the draft lingers.
            try:
                await self._send_draft(part.text, part.parse_mode)
            except Exception:
                self.logger.info("The sync draft failed", exc_info=True)
        part.message = await self.event.reply(
            part.text,
            parse_mode=part.parse_mode,
            link_preview=part.link_preview,
            buttons=part.buttons,
        )


async def on_typing_update(update) -> None:
    """Calls `stop_pressed` on the stream whose draft the user stopped.

    Register it for `UpdateUserTyping` where `STOP_SUPPORTED`, through a
    handler of the plugin's own, so that a plugin reload removes it.
    """
    action = getattr(update, "action", None)
    if not isinstance(action, types.SendMessageStopDraftAction):
        return
    stream = _ACTIVE.get(action.random_id)
    if stream is not None and stream.user_id == update.user_id:
        stream.stop_pressed()
