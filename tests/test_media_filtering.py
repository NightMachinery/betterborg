import asyncio
import builtins
import importlib
import unittest

from uniborg.codex_util import messages_to_codex


class _FakeLoop:
    def create_task(self, coro):
        # llm_chat schedules plugin initialization at import time. Tests only need
        # helper functions, so close the coroutine instead of starting handlers.
        coro.close()
        return None


class _FakeBorg:
    loop = _FakeLoop()


_llm_chat = None


def _load_llm_chat():
    global _llm_chat
    if _llm_chat is not None:
        return _llm_chat

    builtins.borg = _FakeBorg()

    async def _import_module():
        return importlib.import_module("llm_chat_plugins.llm_chat")

    _llm_chat = asyncio.run(_import_module())
    return _llm_chat


class MediaCapabilityFilteringTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.llm_chat = _load_llm_chat()

    def test_repeated_unsupported_video_is_still_skipped(self):
        issued_warnings = {"video"}

        result = self.llm_chat._check_media_capability(
            "video",
            {"video_input": False},
            issued_warnings,
            private_p=True,
        )

        self.assertTrue(result.should_skip)
        self.assertFalse(result.has_warning)
        self.assertEqual(result.warnings, [])

    def test_unknown_mime_is_always_skipped_after_warning(self):
        issued_warnings = {"unknown"}

        result = self.llm_chat._check_media_capability(
            None,
            {},
            issued_warnings,
            private_p=True,
        )

        self.assertTrue(result.should_skip)
        self.assertFalse(result.has_warning)
        self.assertEqual(result.warnings, [])

    def test_private_unknown_mime_emits_first_warning(self):
        issued_warnings = set()

        result = self.llm_chat._check_media_capability(
            None,
            {},
            issued_warnings,
            private_p=True,
        )

        self.assertTrue(result.should_skip)
        self.assertTrue(result.has_warning)
        self.assertEqual(
            result.warnings,
            ["Files with unknown or unsupported media types were skipped."],
        )


class CodexMediaFilteringTests(unittest.TestCase):
    def test_codex_drops_non_image_data_url_parts(self):
        _, converted = messages_to_codex(
            [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image_url",
                            "image_url": {"url": "data:video/webm;base64,AAAA"},
                        },
                        {
                            "type": "image_url",
                            "image_url": {"url": "data:image/png;base64,BBBB"},
                        },
                        {"type": "text", "text": "hello"},
                    ],
                }
            ]
        )

        self.assertEqual(len(converted), 1)
        content = converted[0]["content"]
        self.assertEqual(
            content,
            [
                {
                    "type": "input_image",
                    "image_url": "data:image/png;base64,BBBB",
                    "detail": "low",
                },
                {"type": "input_text", "text": "hello"},
            ],
        )


if __name__ == "__main__":
    unittest.main()
