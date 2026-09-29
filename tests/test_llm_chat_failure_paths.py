import asyncio
import builtins
import importlib
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch


class _FakeLoop:
    def create_task(self, coro):
        coro.close()


builtins.borg = SimpleNamespace(loop=_FakeLoop())


async def _import_plugin():
    return importlib.import_module("llm_chat_plugins.llm_chat")


plugin = asyncio.run(_import_plugin())


def _stream(chunks):
    async def gen():
        for chunk in chunks:
            yield chunk

    return gen()


def _chunk(content=None, finish_reason=None):
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                delta=SimpleNamespace(content=content), finish_reason=finish_reason
            )
        ]
    )


#: What some providers send last: token usage, with no choices at all.
_USAGE_ONLY_CHUNK = SimpleNamespace(choices=[])


class StreamingEdgeTests(unittest.TestCase):
    def _stream_result(self, chunks):
        with patch.object(
            plugin.litellm, "acompletion", new=AsyncMock(return_value=_stream(chunks))
        ):
            return asyncio.run(plugin._call_llm_with_retry(None, None, {}, 1000.0))

    def test_a_stream_with_no_chunks_returns_an_empty_answer(self):
        result = self._stream_result([])
        self.assertEqual(result.text, "")
        self.assertIsNone(result.finish_reason)

    def test_a_usage_only_tail_keeps_the_last_finish_reason(self):
        result = self._stream_result(
            [_chunk("hi"), _chunk(None, "stop"), _USAGE_ONLY_CHUNK]
        )
        self.assertEqual(result.text, "hi")
        self.assertEqual(result.finish_reason, "stop")

    def test_an_ordinary_stream_is_unchanged(self):
        result = self._stream_result([_chunk("a"), _chunk("b", "length")])
        self.assertEqual(result.text, "ab")
        self.assertEqual(result.finish_reason, "length")


if __name__ == "__main__":
    unittest.main()
