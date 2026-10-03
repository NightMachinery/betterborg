"""The partial edits of native Gemini image generation's streaming loop.

Unlike the other streaming loops, this one keeps one pace at any age: the
model's streaming delay and a plain "▌" cursor, with none of
`draft_stream.streaming_pace`'s slower tiers. The stream runs on a scripted
clock (the running loop's `time`, moved only by the fake stream), and
`util.edit_message` is replaced by a recorder.
"""

import asyncio
from contextlib import ExitStack
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from test_llm_chat_stream import plugin


def _chunk(text):
    part = SimpleNamespace(inline_data=None)
    content = SimpleNamespace(parts=[part])
    return SimpleNamespace(candidates=[SimpleNamespace(content=content)], text=text)


class _Models:
    def __init__(self, timeline):
        self.timeline = timeline

    async def generate_content_stream(self, **kwargs):
        async def stream():
            loop = asyncio.get_running_loop()
            for at, text in self.timeline:
                loop.time = lambda at=at: at
                yield _chunk(text)

        return stream()


def _run(timeline, *, edit_interval):
    edits = []

    async def edit_message(message, text, **kwargs):
        edits.append((message, text, kwargs))

    client = SimpleNamespace(aio=SimpleNamespace(models=_Models(timeline)))
    event = SimpleNamespace(sender_id=31, chat_id=31)

    async def main():
        loop = asyncio.get_running_loop()
        loop.time = lambda: 0.0
        try:
            return await plugin._handle_native_gemini_image_generation(
                event,
                [{"role": "user", "content": "draw"}],
                "key",
                "gemini/image-model",
                "placeholder",
                {},
            )
        finally:
            del loop.time

    with ExitStack() as stack:
        for target, name, value in (
            (plugin.llm_util, "create_genai_client", lambda **kwargs: client),
            (plugin, "_get_effective_model_and_service", lambda *a, **k: ("m", "")),
            (plugin, "get_streaming_delay", lambda model: edit_interval),
            (plugin, "_gemini_convert_messages_with_history", lambda messages: []),
            (plugin, "_thread_topic_id", lambda event: None),
            (plugin.util, "edit_message", edit_message),
        ):
            stack.enter_context(patch.object(target, name, value))
        result = asyncio.run(main())
    return result, edits


class GeminiImageStreamTests(unittest.TestCase):
    def test_edits_keep_one_pace_and_cursor_at_any_age(self):
        timeline = [
            (0.5, "a"),
            (0.9, "b"),
            (1.5, "c"),
            (1.8, "d"),
            (40.0, "e"),
            (40.5, "f"),
            (41.0, "g"),
        ]

        result, edits = _run(timeline, edit_interval=0.8)

        self.assertEqual(result, ("abcdefg", False))
        self.assertEqual(
            edits,
            [
                ("placeholder", "ab▌", {"parse_mode": "md"}),
                ("placeholder", "abcd▌", {"parse_mode": "md"}),
                ("placeholder", "abcde▌", {"parse_mode": "md"}),
                ("placeholder", "abcdefg▌", {"parse_mode": "md"}),
            ],
        )


if __name__ == "__main__":
    unittest.main()
