"""Golden characterization tests for chat response delivery.

The delivery refactor moves every step of delivering an answer (the `...`
placeholder, streaming partial edits, the final edit, and the cancel and
error notes) behind a shared core, `uniborg/stream_driver.py`, and it must
not change what Telegram sees. The partial edits of every streaming loop
already go through its shared throttle, `stream_driver.PacedEditor`. These
tests pin what Telegram sees today.

Terms used below:

- A *delivery log* is the ordered list of Telegram-facing calls one request
  makes. Each entry is `(name, *positional_args, kwargs)`. Objects the test
  created are replaced by tokens, so an entry reads `"placeholder"` or
  `"event.message"` instead of an object repr.
- A *golden* is the delivery log a test expects, written out literally.
- The *timeline* is a fake backend stream: text deltas, each arriving at an
  exact number of seconds after the stream opened.
- The *scripted clock* replaces the running event loop's `time()` for one
  request. It only moves when the timeline says so, so the throttle sees exact
  timestamps whether it reads `asyncio.get_event_loop().time()` or
  `asyncio.get_running_loop().time()`. It is frozen between events, so code
  under test must not sleep on the loop while it is installed; a wall-clock
  watchdog turns such a sleep into a failure instead of a hang. If the
  refactor reads a different clock, point the scripted clock at that one
  instead of changing a golden.

The log is recorded at the boundaries the refactor has to keep:

- `event.reply` for the placeholder, which is what
  `send_info_message(event, "...")` sends;
- `uniborg.util.edit_message` for partial, final and note edits;
- the placeholder's `delete`;
- `event.client.send_file` for generated images.

Anything between those boundaries (which helper streams, which one retries)
is free to change. Everything at them is pinned: an intentional change to a
delivery log must update its golden here, explicitly, in the same commit.
"""

import asyncio
import base64
import builtins
import importlib
import io
import threading
import unittest
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Callable, Optional
from unittest.mock import AsyncMock, patch

from PIL import Image

from test_codex_streaming import _Client, _Stream, _completed, _image_item
from uniborg import codex_util, llm_chat_config, pioneer_util, util
from uniborg.constants import (
    BOT_META_INFO_LINE,
    BOT_META_INFO_PREFIX,
    DEFAULT_FILE_LENGTH_THRESHOLD,
    DEFAULT_FILE_ONLY_LENGTH_THRESHOLD,
    OPENAI_CODEX_SOL,
    PIONEER_GPT_5_5,
)


class _FakeLoop:
    def create_task(self, coro):
        coro.close()
        return None


class _FakeBorg:
    loop = _FakeLoop()


class _Action:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False


class _ActingBorg(_FakeBorg):
    """Enough of `borg` for `_send_image_to_telegram`'s upload action."""

    def action(self, *args, **kwargs):
        return _Action()


async def _import_llm_chat():
    return importlib.import_module("llm_chat_plugins.llm_chat")


builtins.borg = _FakeBorg()
plugin = asyncio.run(_import_llm_chat())


#: IDs with no stored preferences, so nothing on disk leaks into a golden.
_USER_ID = 999000556
_CHAT_ID = 999000557
_EVENT_ID = 4242
_PLACEHOLDER_ID = 4343
_GEMINI_KEY = "gemini-key"

#: Any litellm model outside the Gemini, Codex and Pioneer branches whose
#: streaming delay is the 0.8 s default.
_LITELLM_MODEL = "openrouter/test/model"


# --- Delivery log ---


class _DeliveryLog:
    """Records one request's Telegram-facing delivery calls, in order."""

    def __init__(self):
        self.calls = []
        self._tokens = []

    def token(self, obj, name: str):
        self._tokens.append((obj, name))
        return obj

    def _plain(self, value):
        for obj, name in self._tokens:
            if value is obj:
                return name
        if isinstance(value, io.BytesIO):
            #: Uploaded files are identified by the name Telegram shows.
            return getattr(value, "name", "<unnamed file>")
        return value

    def record(self, name: str, *args, **kwargs):
        self.calls.append(
            (
                name,
                *(self._plain(arg) for arg in args),
                {key: self._plain(value) for key, value in kwargs.items()},
            )
        )

    async def edit_message(self, message, text, **kwargs):
        self.record("edit", message, text, **kwargs)


class _SentMessage:
    def __init__(self, log: _DeliveryLog, message_id: int):
        self.id = message_id
        self._log = log

    async def delete(self, **kwargs):
        self._log.record("delete", self, **kwargs)


def _event(log: _DeliveryLog, *, text: str, is_private: bool):
    placeholder = log.token(_SentMessage(log, _PLACEHOLDER_ID), "placeholder")
    message = log.token(SimpleNamespace(id=_EVENT_ID, text=text), "event.message")

    async def reply(reply_text, **kwargs):
        log.record("reply", reply_text, **kwargs)
        return placeholder

    async def respond(respond_text, **kwargs):
        #: Not expected today; recorded so a golden diff shows it. It returns
        #: the same placeholder, so moving the placeholder to `respond` only
        #: changes `PLACEHOLDER_SENT`, not every edit after it.
        log.record("respond", respond_text, **kwargs)
        return placeholder

    async def send_file(entity, **kwargs):
        log.record("send_file", entity, **kwargs)

    return SimpleNamespace(
        id=_EVENT_ID,
        sender_id=_USER_ID,
        chat_id=_CHAT_ID,
        chat=object(),
        grouped_id=None,
        is_private=is_private,
        text=text,
        file=None,
        message=message,
        reply=reply,
        respond=respond,
        client=SimpleNamespace(send_file=send_file),
    )


PLACEHOLDER_SENT = ("reply", f"{BOT_META_INFO_PREFIX}...", {})
PLACEHOLDER_DELETED = ("delete", "placeholder", {})
CANCEL_NOTE = (
    "edit",
    "placeholder",
    f"{BOT_META_INFO_PREFIX}❌ Request was canceled.",
    {"append_p": True, "parse_mode": "md"},
)


def partial_edit(text: str):
    return ("edit", "placeholder", text, {"parse_mode": "md"})


def final_edit(
    text: str, *, file_only_threshold: int = DEFAULT_FILE_ONLY_LENGTH_THRESHOLD
):
    return (
        "edit",
        "placeholder",
        text,
        {
            "parse_mode": "md",
            "link_preview": False,
            "send_file_mode": util.SendFileMode.ALSO_IF_LESS_THAN,
            "file_length_threshold": DEFAULT_FILE_LENGTH_THRESHOLD,
            "file_only_threshold": file_only_threshold,
            "file_name_mode": "llm",
            "title_generator": f"title_generator(user={_USER_ID}, codex=True)",
            "reply_to": "event.message",
            "send_new_on_head_failure": True,
        },
    )


# --- Scripted clock and streams ---


class _ScriptedClock:
    def __init__(self, now: float = 0.0):
        self.now = now

    def time(self) -> float:
        return self.now


@contextmanager
def _clock_on_running_loop(clock: _ScriptedClock):
    loop = asyncio.get_running_loop()
    loop.time = clock.time
    try:
        yield
    finally:
        del loop.time


#: Wall-clock seconds one scripted run may take; a normal one takes well under
#: one. Loop timers never fire on the frozen scripted clock, so without this a
#: regression that sleeps, or a stream that nothing cancels, hangs the suite.
_STALL_SECONDS = 10.0


def _run_on_scripted_clock(main, *, clock: _ScriptedClock):
    """Run the coroutine MAIN on a fresh loop that reads CLOCK.

    A run still going after `_STALL_SECONDS` is cancelled and fails.
    """
    stalled = threading.Event()

    async def guarded():
        loop = asyncio.get_running_loop()
        task = asyncio.current_task()

        def stall():
            stalled.set()
            try:
                #: Thread-safe and timer-free, so it lands on a frozen loop.
                loop.call_soon_threadsafe(task.cancel)
            except RuntimeError:
                #: The loop closed as the timer fired, so the run is over.
                pass

        watchdog = threading.Timer(_STALL_SECONDS, stall)
        watchdog.daemon = True
        watchdog.start()
        try:
            with _clock_on_running_loop(clock):
                return await main
        finally:
            watchdog.cancel()

    try:
        return asyncio.run(guarded())
    finally:
        if stalled.is_set():
            raise AssertionError(
                f"The run stalled for {_STALL_SECONDS} s: code under test slept "
                "on the frozen scripted clock, or waited on something that "
                "nothing ever finished or cancelled."
            )


class _ScriptedStream(_Stream):
    """Moves the scripted clock to each event's time before yielding it.

    With `parked`, it then sets that event and waits until cancelled, like a
    model that stalls mid-answer.
    """

    def __init__(self, clock, timed_events, *, parked=None):
        super().__init__()
        self.clock = clock
        self.timed_events = list(timed_events)
        self.parked = parked

    async def _iterate(self):
        for at, event in self.timed_events:
            self.clock.now = at
            yield event
        if self.parked is not None:
            self.parked.set()
            await asyncio.get_running_loop().create_future()


def _litellm_chunk(content, *, finish_reason=None):
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                delta=SimpleNamespace(content=content), finish_reason=finish_reason
            )
        ]
    )


def _codex_delta(text):
    return {
        "type": "response.output_text.delta",
        "item_id": "message-a",
        "output_index": 0,
        "content_index": 0,
        "delta": text,
    }


def _pioneer_delta(text):
    return SimpleNamespace(type="response.output_text.delta", delta=text)


def _pioneer_completed():
    return SimpleNamespace(
        type="response.completed", response=SimpleNamespace(status="completed")
    )


def _serve_from_litellm(stack: ExitStack, stream):
    stack.enter_context(
        patch.object(plugin.litellm, "acompletion", new=AsyncMock(return_value=stream))
    )


def _litellm_completion(content, *, finish_reason=None):
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(content=content), finish_reason=finish_reason
            )
        ]
    )


def _serve_litellm_completion(stack: ExitStack, stream):
    """Serve the timeline's text as one completion, as litellm does with
    `stream=False`. The scripted clock never moves, since nothing streams."""
    chunks = [event for _, event in stream.timed_events]
    completion = _litellm_completion(
        "".join(chunk.choices[0].delta.content or "" for chunk in chunks),
        finish_reason=chunks[-1].choices[0].finish_reason if chunks else None,
    )
    stack.enter_context(
        patch.object(
            plugin.litellm, "acompletion", new=AsyncMock(return_value=completion)
        )
    )


def _serve_from_client_of(module):
    def serve(stack: ExitStack, stream):
        stack.enter_context(
            patch.object(
                module,
                "_create_async_client",
                new=AsyncMock(return_value=_Client(stream)),
            )
        )

    return serve


@dataclass(frozen=True)
class _Backend:
    """One way a chat request reaches a model, as far as a fake stream cares."""

    model: str
    service: str
    admin: bool
    delta: Callable[[Optional[str]], object]
    done: Callable[[], object]
    serve: Callable[[ExitStack, object], None]
    #: An image model is called without streaming.
    image_generation: bool = False


LITELLM = _Backend(
    model=_LITELLM_MODEL,
    service="openrouter",
    admin=False,
    delta=_litellm_chunk,
    done=lambda: _litellm_chunk(None, finish_reason="stop"),
    serve=_serve_from_litellm,
)
#: A litellm image model: the answer's images arrive as `data:image/...` URLs
#: in the text.
LITELLM_IMAGE = _Backend(
    model=_LITELLM_MODEL,
    service="openrouter",
    admin=False,
    delta=_litellm_chunk,
    done=lambda: _litellm_chunk(None, finish_reason="stop"),
    serve=_serve_litellm_completion,
    image_generation=True,
)
CODEX = _Backend(
    model=OPENAI_CODEX_SOL,
    service="codex",
    admin=False,
    delta=_codex_delta,
    done=_completed,
    serve=_serve_from_client_of(codex_util),
)
PIONEER = _Backend(
    model=PIONEER_GPT_5_5,
    service="pioneer",
    #: Pioneer models are admin-only.
    admin=True,
    delta=_pioneer_delta,
    done=_pioneer_completed,
    serve=_serve_from_client_of(pioneer_util),
)


#: (seconds since the stream opened, text delta). Every backend streams at the
#: 0.8 s default interval, slows to 15 s ("▌💤") after 30 s and to 60 s
#: ("▌💤💤") after 120 s. Every comparison is strict, which the exact
#: boundary steps below pin.
TIERED_TIMELINE = (
    (0.5, "Hel"),  #: 0.5 s since the stream opened: no edit.
    (1.0, "lo"),  #: 1.0 s > 0.8 s: edit.
    (1.5, " wor"),  #: 0.5 s since the last edit: no edit.
    (2.0, "ld"),  #: edit.
    (10.0, None),  #: No text. Only Codex edits here, with unchanged text.
    (30.0, "."),  #: Exactly 30 s is still the normal tier: edit, plain cursor.
    (31.0, " A"),  #: Slow tier; 1 s since the last edit: no edit.
    (45.0, " B"),  #: Exactly 15 s since the last edit: no edit.
    (45.5, " C"),  #: 15.5 s: edit.
    (120.0, " D"),  #: Exactly 120 s is still the slow tier: edit.
    (121.0, " E"),  #: Doubly slow tier; 1 s since the last edit: no edit.
    (180.0, " F"),  #: Exactly 60 s since the last edit: no edit.
    (180.5, " G"),  #: 60.5 s: edit.
)
TIERED_ANSWER = "Hello world. A B C D E F G"
TIERED_PARTIALS = (
    "Hello▌",
    "Hello world▌",
    "Hello world.▌",
    "Hello world. A B C▌💤",
    "Hello world. A B C D▌💤",
    "Hello world. A B C D E F G▌💤💤",
)
#: Codex checks the interval on every delta event, even one without text,
#: while litellm and Pioneer skip those.
CODEX_TIERED_PARTIALS = (
    "Hello▌",
    "Hello world▌",
    "Hello world▌",
    "Hello world.▌",
    "Hello world. A B C▌💤",
    "Hello world. A B C D▌💤",
    "Hello world. A B C D E F G▌💤💤",
)
#: Well past every step, so the terminal event would edit if it could.
DONE_AT = 400.0


def _timed_events(backend: _Backend, timeline, *, done: bool = True):
    events = [(at, backend.delta(text)) for at, text in timeline]
    if done:
        events.append((DONE_AT, backend.done()))
    return events


def _png_b64() -> str:
    buffer = io.BytesIO()
    Image.new("RGB", (1, 1), "red").save(buffer, format="PNG")
    return base64.b64encode(buffer.getvalue()).decode("ascii")


# --- Driving chat_handler ---


def _text_only_capabilities(*, image_generation: bool = False):
    """Text is the only input; images are an output with IMAGE_GENERATION."""
    return {
        "vision": False,
        "audio_input": False,
        "video_input": False,
        "audio_output": False,
        "pdf_input": False,
        "image_generation": image_generation,
    }


def _run_chat(
    backend: _Backend,
    *,
    timed_events,
    text: str = "hello",
    is_private: bool = True,
    warnings=(),
    stop_when_parked: bool = False,
    reasoning_level: Optional[str] = None,
    schedule_topic_title=None,
) -> list:
    """Run one message through `chat_handler` and return its delivery log.

    With `stop_when_parked`, the stream parks after its events and the test
    then cancels the user's LLM tasks, which is what /stop does.
    `schedule_topic_title`, when given, replaces `_schedule_topic_title`.
    """
    log = _DeliveryLog()
    event = _event(log, text=text, is_private=is_private)
    clock = _ScriptedClock()
    parked = asyncio.Event() if stop_when_parked else None
    stream = _ScriptedStream(clock, timed_events, parked=parked)

    with ExitStack() as stack:
        enter = stack.enter_context
        enter(patch.object(builtins, "borg", _ActingBorg()))
        enter(patch.object(plugin, "ACTIVE_LLM_TASKS", {}))
        enter(patch.object(plugin, "AWAITING_INPUT_FROM_USERS", {}))
        enter(patch.object(plugin, "WARN_UNSUPPORTED_TO_USER_P", "private_only"))
        enter(patch.object(plugin.llm_db, "is_awaiting_key", return_value=False))
        enter(
            patch.object(
                plugin.gemini_live_util.live_session_manager,
                "is_live_mode_active",
                return_value=False,
            )
        )
        enter(
            patch.object(
                plugin.user_manager,
                "get_prefs",
                return_value=plugin.UserPrefs(enabled_tools=[]),
            )
        )
        enter(
            patch.object(
                plugin.util, "isAdmin", new=AsyncMock(return_value=backend.admin)
            )
        )
        enter(
            patch.object(
                plugin.llm_chat_config,
                "load_config",
                return_value=llm_chat_config.LLMChatConfig((_USER_ID,), (_USER_ID,)),
            )
        )
        enter(
            patch.object(
                plugin,
                "_determine_context_mode_and_handle_transitions",
                new=AsyncMock(return_value="reply_chain"),
            )
        )
        enter(
            patch.object(
                plugin,
                "_get_effective_model_and_service",
                side_effect=lambda *args, prefix_model=None, topic_id=None: (
                    prefix_model or backend.model,
                    backend.service,
                ),
            )
        )
        enter(patch.object(plugin, "get_effective_api_key", return_value="test-key"))
        enter(
            patch.object(
                plugin, "get_effective_gemini_api_key", return_value=_GEMINI_KEY
            )
        )
        #: The generator is a closure; the log names it by what it was built for.
        enter(
            patch.object(
                plugin,
                "_file_title_generator",
                side_effect=lambda user_id, *, codex_p: log.token(
                    object(), f"title_generator(user={user_id}, codex={codex_p})"
                ),
            )
        )
        #: Keeps litellm's model map out of the goldens; text-only means the
        #: litellm branch streams, unless the backend generates images.
        enter(
            patch.object(
                plugin,
                "get_model_capabilities",
                return_value=_text_only_capabilities(
                    image_generation=backend.image_generation
                ),
            )
        )
        enter(
            patch.object(
                plugin,
                "_get_effective_reasoning",
                return_value=SimpleNamespace(level=reasoning_level),
            )
        )
        if schedule_topic_title is not None:
            enter(
                patch.object(plugin, "_schedule_topic_title", new=schedule_topic_title)
            )
        enter(
            patch.object(
                plugin,
                "build_conversation_history",
                new=AsyncMock(
                    return_value=plugin.ConversationHistoryResult(
                        history=[{"role": "user", "content": text}],
                        warnings=list(warnings),
                    )
                ),
            )
        )
        enter(patch.object(plugin, "_handle_tts_response", new=AsyncMock()))
        enter(patch.object(plugin, "_log_conversation", new=AsyncMock()))
        enter(patch.object(plugin.util, "edit_message", new=log.edit_message))
        backend.serve(stack, stream)
        _run_on_scripted_clock(_drive(event, parked=parked), clock=clock)
    return log.calls


async def _drive(event, *, parked: Optional[asyncio.Event]):
    handler = asyncio.ensure_future(plugin.chat_handler(event))
    if parked is not None:
        #: No timeout: the scripted clock is frozen, so timers never fire.
        waiter = asyncio.ensure_future(parked.wait())
        await asyncio.wait({handler, waiter}, return_when=asyncio.FIRST_COMPLETED)
        waiter.cancel()
        if parked.is_set():
            stopped = plugin.cancel_all_llm_tasks(event.sender_id)
            #: /stop answers "Stopped 1 active chat request(s).". Any other
            #: count is a visible change, and with none the stream never ends.
            if stopped != 1:
                handler.cancel()
                raise AssertionError(
                    f"/stop found {stopped} tracked LLM task(s) for one request, "
                    "not 1."
                )
    await handler


# --- Tests ---


class LitellmStreamingLoopGoldenTests(unittest.TestCase):
    """`_call_llm_with_retry` on its own: the partial edits and its result.

    The only test here that calls an internal function directly. If the
    refactor changes its signature, adapt the call, not the golden.
    """

    def test_partial_edits_follow_the_tiers_and_the_answer_is_returned(self):
        log = _DeliveryLog()
        placeholder = log.token(_SentMessage(log, _PLACEHOLDER_ID), "placeholder")
        clock = _ScriptedClock()
        stream = _ScriptedStream(clock, _timed_events(LITELLM, TIERED_TIMELINE))

        with ExitStack() as stack:
            stack.enter_context(
                patch.object(plugin.util, "edit_message", new=log.edit_message)
            )
            _serve_from_litellm(stack, stream)
            result = _run_on_scripted_clock(
                plugin._call_llm_with_retry(
                    SimpleNamespace(id=_EVENT_ID),
                    placeholder,
                    {"model": _LITELLM_MODEL, "messages": [], "stream": True},
                    0.8,
                ),
                clock=clock,
            )

        self.assertEqual(log.calls, [partial_edit(text) for text in TIERED_PARTIALS])
        self.assertEqual(result.text, TIERED_ANSWER)
        self.assertEqual(result.finish_reason, "stop")
        self.assertFalse(result.has_image)


class StreamingDeliveryGoldenTests(unittest.TestCase):
    """Placeholder, tiered partial edits, then the final edit, per backend."""

    def assert_golden(self, backend: _Backend, partials):
        calls = _run_chat(backend, timed_events=_timed_events(backend, TIERED_TIMELINE))
        self.assertEqual(
            calls,
            [
                PLACEHOLDER_SENT,
                *(partial_edit(text) for text in partials),
                final_edit(TIERED_ANSWER),
            ],
        )

    def test_litellm(self):
        self.assert_golden(LITELLM, TIERED_PARTIALS)

    def test_pioneer(self):
        self.assert_golden(PIONEER, TIERED_PARTIALS)

    def test_codex(self):
        self.assert_golden(CODEX, CODEX_TIERED_PARTIALS)


class TopicTitleHandoffTests(unittest.TestCase):
    """What a delivered answer hands to the automatic topic title."""

    def test_the_answer_its_model_and_effort_are_handed_over(self):
        schedule = AsyncMock()
        timeline = ((0.1, "  Hi there.  "),)

        _run_chat(
            LITELLM,
            text=".fl hello",
            timed_events=_timed_events(LITELLM, timeline),
            reasoning_level="high",
            schedule_topic_title=schedule,
        )

        schedule.assert_awaited_once()
        self.assertEqual(
            schedule.await_args.kwargs,
            {
                "question": "hello",
                "answer": "  Hi there.  ",
                "model": plugin.GEMINI_FLASH_LITE_LATEST,
                "reasoning_level": "high",
                "codex_p": True,
            },
        )


class FinalDeliveryGoldenTests(unittest.TestCase):
    #: Groups send long answers as a file alone sooner than private chats.
    GROUP_FILE_ONLY_THRESHOLD = 8000

    def run_short_litellm_answer(self, **kwargs):
        #: One delta well inside the first interval, so no partial edit.
        timeline = ((0.1, "  Hi there.  "),)
        return _run_chat(
            LITELLM, timed_events=_timed_events(LITELLM, timeline), **kwargs
        )

    def test_private_chat_final_edit(self):
        calls = self.run_short_litellm_answer()
        #: The final text is stripped.
        self.assertEqual(calls, [PLACEHOLDER_SENT, final_edit("Hi there.")])

    def test_group_final_edit(self):
        calls = self.run_short_litellm_answer(is_private=False)
        self.assertEqual(
            calls,
            [
                PLACEHOLDER_SENT,
                final_edit(
                    "Hi there.", file_only_threshold=self.GROUP_FILE_ONLY_THRESHOLD
                ),
            ],
        )

    def test_warnings_ride_on_the_final_edit_in_private_chats(self):
        calls = self.run_short_litellm_answer(warnings=("second", "first", "second"))
        footer = f"\n\n{BOT_META_INFO_LINE}\n**Note:**\n- first\n- second"
        self.assertEqual(calls, [PLACEHOLDER_SENT, final_edit(f"Hi there.{footer}")])

    def test_warnings_are_dropped_in_groups(self):
        calls = self.run_short_litellm_answer(is_private=False, warnings=("first",))
        self.assertEqual(
            calls,
            [
                PLACEHOLDER_SENT,
                final_edit(
                    "Hi there.", file_only_threshold=self.GROUP_FILE_ONLY_THRESHOLD
                ),
            ],
        )

    #: A litellm image model's picture, sent before the text is delivered.
    LITELLM_IMAGE_SENT = (
        "send_file",
        _CHAT_ID,
        {"file": "generated_image.png", "reply_to": _EVENT_ID},
    )

    def run_litellm_image_answer(self, *texts):
        timeline = [(0.1 * (i + 1), text) for i, text in enumerate(texts)]
        return _run_chat(
            LITELLM_IMAGE, timed_events=_timed_events(LITELLM_IMAGE, timeline)
        )

    def test_image_only_answer_deletes_the_placeholder(self):
        #: Codex's `.i` image generation.
        item = _image_item("image-a", _png_b64())
        calls = _run_chat(
            CODEX,
            text=".i draw",
            timed_events=[(0.1, _completed([item]))],
        )
        self.assertEqual(
            calls,
            [
                PLACEHOLDER_SENT,
                (
                    "send_file",
                    _CHAT_ID,
                    {"file": "codex_generated_image_1.png", "reply_to": _EVENT_ID},
                ),
                PLACEHOLDER_DELETED,
            ],
        )

    def test_litellm_image_only_answer_deletes_the_placeholder(self):
        #: The data URL is sent as a file and never edited into the placeholder.
        calls = self.run_litellm_image_answer(f"data:image/png;base64,{_png_b64()}")
        self.assertEqual(
            calls, [PLACEHOLDER_SENT, self.LITELLM_IMAGE_SENT, PLACEHOLDER_DELETED]
        )

    def test_litellm_image_with_text_edits_in_the_text_alone(self):
        calls = self.run_litellm_image_answer(
            "Here it is: ", f"data:image/png;base64,{_png_b64()}"
        )
        self.assertEqual(
            calls,
            [PLACEHOLDER_SENT, self.LITELLM_IMAGE_SENT, final_edit("Here it is:")],
        )

    def test_empty_pioneer_answer_deletes_the_placeholder(self):
        calls = _run_chat(PIONEER, timed_events=_timed_events(PIONEER, ()))
        self.assertEqual(calls, [PLACEHOLDER_SENT, PLACEHOLDER_DELETED])


class CancelAndErrorGoldenTests(unittest.TestCase):
    #: The first two steps of the tiered timeline: one partial edit, "Hello▌".
    OPENING = TIERED_TIMELINE[:2]

    def assert_stop_golden(self, backend: _Backend):
        calls = _run_chat(
            backend,
            timed_events=_timed_events(backend, self.OPENING, done=False),
            stop_when_parked=True,
        )
        #: No final edit follows: chat_handler swallows the cancellation.
        self.assertEqual(calls, [PLACEHOLDER_SENT, partial_edit("Hello▌"), CANCEL_NOTE])

    def test_stop_appends_the_cancel_note_litellm(self):
        self.assert_stop_golden(LITELLM)

    def test_stop_appends_the_cancel_note_pioneer(self):
        self.assert_stop_golden(PIONEER)

    def test_stop_appends_the_cancel_note_codex(self):
        self.assert_stop_golden(CODEX)

    def test_codex_failure_replaces_the_partial_with_partial_and_error(self):
        #: The stream ends without `response.completed`.
        calls = _run_chat(
            CODEX, timed_events=_timed_events(CODEX, self.OPENING, done=False)
        )
        error = (
            f"Hello\n\n{BOT_META_INFO_LINE}\n"
            f"{BOT_META_INFO_PREFIX}❌ Codex request failed.\n"
            "```\nCodex stream ended before response completion.\n```"
        )
        self.assertEqual(
            calls,
            [
                PLACEHOLDER_SENT,
                partial_edit("Hello▌"),
                ("edit", "placeholder", error, {"parse_mode": "md"}),
            ],
        )


if __name__ == "__main__":
    unittest.main()
