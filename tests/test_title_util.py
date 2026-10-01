"""`title_util`: which model writes a title, and what happens when it fails."""

import asyncio
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest import mock

from pydantic import BaseModel

from uniborg import codex_util, title_util, util
from uniborg.constants import GEMINI_FLASH_LITE_LATEST, OPENAI_CODEX_LUNA_RESERVE

T0 = datetime(2026, 10, 1, 12, tzinfo=timezone.utc)
OTHER_MODEL = "openrouter/some/model"


class _Title(BaseModel):
    title: str


def _usage_limit(resets_at=None):
    return codex_util.CodexStreamError(
        "spent",
        codex_util.CodexResponse(text=""),
        usage_limit=codex_util.CodexUsageLimit(resets_at=resets_at),
    )


class ResolveTitleModelTests(unittest.TestCase):
    def test_auto_is_the_reserve_with_codex_and_flash_lite_without(self):
        for choice in ("auto", None, ""):
            with self.subTest(choice=choice):
                self.assertEqual(
                    title_util.resolve_title_model(choice, codex_p=True),
                    OPENAI_CODEX_LUNA_RESERVE,
                )
                self.assertEqual(
                    title_util.resolve_title_model(choice, codex_p=False),
                    GEMINI_FLASH_LITE_LATEST,
                )

    def test_a_codex_choice_without_codex_access_is_flash_lite(self):
        self.assertEqual(
            title_util.resolve_title_model(OPENAI_CODEX_LUNA_RESERVE, codex_p=False),
            GEMINI_FLASH_LITE_LATEST,
        )

    def test_any_other_choice_is_kept(self):
        self.assertEqual(
            title_util.resolve_title_model(OTHER_MODEL, codex_p=False), OTHER_MODEL
        )


class ParseJsonReplyTests(unittest.TestCase):
    def test_plain_fenced_and_wrapped_replies(self):
        for reply in (
            '{"title": "A"}',
            '```json\n{"title": "A"}\n```',
            'Here it is: {"title": "A"} Hope that helps.',
        ):
            with self.subTest(reply=reply):
                self.assertEqual(
                    title_util.parse_json_reply(reply, _Title), _Title(title="A")
                )

    def test_a_reply_without_an_object_raises(self):
        with self.assertRaises(ValueError):
            title_util.parse_json_reply("no json here", _Title)


class GenerateTitleTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.now = T0
        self.pause = title_util.CodexTitlePause(clock=lambda: self.now)
        self.calls = []
        self.failing = {}
        stored = mock.patch.object(
            title_util.llm_db, "get_api_key", return_value="stored-key"
        )
        self.get_api_key = stored.start()
        self.addCleanup(stored.stop)

    async def complete(self, prompt, schema, *, model, api_key, api_user_id):
        self.calls.append((model, api_key))
        error = self.failing.get(model)
        if error is not None:
            raise error
        return schema(title=model)

    async def generate(self, *, choice="auto", codex_p=True, **kwargs):
        kwargs.setdefault("api_keys", {"gemini": "gemini-key"})
        return await title_util.generate_title(
            "prompt",
            _Title,
            choice=choice,
            codex_p=codex_p,
            api_user_id=5,
            complete=self.complete,
            codex_pause=self.pause,
            **kwargs,
        )

    async def test_auto_with_codex_asks_the_reserve_without_a_key(self):
        title = await self.generate()

        self.assertEqual(title.title, OPENAI_CODEX_LUNA_RESERVE)
        self.assertEqual(self.calls, [(OPENAI_CODEX_LUNA_RESERVE, None)])

    async def test_a_failed_reserve_falls_back_to_flash_lite(self):
        self.failing[OPENAI_CODEX_LUNA_RESERVE] = RuntimeError("down")

        title = await self.generate()

        self.assertEqual(title.title, GEMINI_FLASH_LITE_LATEST)
        self.assertEqual(
            self.calls,
            [
                (OPENAI_CODEX_LUNA_RESERVE, None),
                (GEMINI_FLASH_LITE_LATEST, "gemini-key"),
            ],
        )
        self.assertFalse(self.pause.active())

    async def test_a_spent_reserve_is_skipped_until_it_resets(self):
        self.failing[OPENAI_CODEX_LUNA_RESERVE] = _usage_limit(
            resets_at=T0 + timedelta(hours=2)
        )
        await self.generate()
        self.calls.clear()

        await self.generate()
        self.assertEqual(self.calls, [(GEMINI_FLASH_LITE_LATEST, "gemini-key")])

        self.now = T0 + timedelta(hours=3)
        self.calls.clear()
        await self.generate()
        self.assertEqual(self.calls[0], (OPENAI_CODEX_LUNA_RESERVE, None))

    async def test_a_usage_limit_without_a_reset_pauses_for_an_hour(self):
        self.failing[OPENAI_CODEX_LUNA_RESERVE] = _usage_limit()

        await self.generate()

        self.assertEqual(self.pause.until, T0 + title_util.CODEX_PAUSE_WITHOUT_RESET)

    async def test_without_codex_auto_is_flash_lite_alone(self):
        await self.generate(codex_p=False)

        self.assertEqual(self.calls, [(GEMINI_FLASH_LITE_LATEST, "gemini-key")])

    async def test_another_model_uses_the_stored_key_then_falls_back(self):
        self.failing[OTHER_MODEL] = RuntimeError("no structured output")

        await self.generate(choice=OTHER_MODEL)

        self.get_api_key.assert_called_with(5, service="openrouter")
        self.assertEqual(
            self.calls,
            [(OTHER_MODEL, "stored-key"), (GEMINI_FLASH_LITE_LATEST, "gemini-key")],
        )

    async def test_a_slow_model_times_out_and_falls_back(self):
        async def complete(prompt, schema, *, model, api_key, api_user_id):
            if model == OPENAI_CODEX_LUNA_RESERVE:
                await asyncio.sleep(1)
            return schema(title=model)

        title = await title_util.generate_title(
            "prompt",
            _Title,
            choice="auto",
            codex_p=True,
            api_keys={"gemini": "gemini-key"},
            timeout=0.05,
            complete=complete,
            codex_pause=self.pause,
        )

        self.assertEqual(title.title, GEMINI_FLASH_LITE_LATEST)

    async def test_nothing_left_to_try_raises(self):
        self.failing[OPENAI_CODEX_LUNA_RESERVE] = RuntimeError("down")
        self.get_api_key.return_value = None

        with self.assertRaises(title_util.TitleUnavailableError) as raised:
            await self.generate(api_keys={})

        self.assertIn("no API key", str(raised.exception))


class CompleteStructuredTests(unittest.IsolatedAsyncioTestCase):
    async def test_codex_gets_the_schema_in_its_instructions(self):
        complete_text = mock.AsyncMock(return_value='```json\n{"title": "T"}\n```')
        with mock.patch.object(
            title_util.codex_util, "complete_codex_text", complete_text
        ):
            title = await title_util.complete_structured(
                "prompt", _Title, model=OPENAI_CODEX_LUNA_RESERVE
            )

        self.assertEqual(title, _Title(title="T"))
        kwargs = complete_text.await_args.kwargs
        self.assertEqual(kwargs["text"], "prompt")
        self.assertEqual(kwargs["reasoning_effort"], "low")
        self.assertIn('"title"', kwargs["instructions"])

    async def test_gemini_goes_through_litellm_and_the_proxy(self):
        response = SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content='{"title": "T"}'))]
        )
        acompletion = mock.AsyncMock(return_value=response)
        proxy = object()
        with mock.patch.object(
            title_util.litellm, "acompletion", acompletion
        ), mock.patch.object(
            title_util.llm_util, "create_litellm_proxy_client", return_value=proxy
        ) as create_proxy:
            title = await title_util.complete_structured(
                "prompt",
                _Title,
                model=GEMINI_FLASH_LITE_LATEST,
                api_key="key",
                api_user_id=5,
            )

        self.assertEqual(title, _Title(title="T"))
        create_proxy.assert_called_once_with(5)
        kwargs = acompletion.await_args.kwargs
        self.assertEqual(kwargs["api_key"], "key")
        self.assertIs(kwargs["response_format"], _Title)
        self.assertIs(kwargs["client"], proxy)


class FileDataTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_title_generator_names_the_file(self):
        generator = mock.AsyncMock(
            return_value=util.FilenameGeneration(
                title="Monads", title_as_file_name="monads", short_description="On M."
            )
        )

        data = await util._generate_file_data(
            "x" * 20000, "md", "llm", title_generator=generator
        )

        self.assertEqual(data.filename, "monads.md")
        self.assertEqual(data.caption, "**Monads**\n\nOn M.")
        self.assertLessEqual(len(generator.await_args.args[0]), 10000)

    async def test_a_failing_title_generator_gives_a_random_name(self):
        generator = mock.AsyncMock(side_effect=title_util.TitleUnavailableError("x"))

        data = await util._generate_file_data(
            "text", "md", "llm", title_generator=generator, default_caption=""
        )

        self.assertTrue(data.filename.startswith("message_"))
        self.assertIn("Failed to generate a title", data.caption)

    async def test_without_a_generator_or_key_the_name_is_random(self):
        with mock.patch.object(util, "_resolve_title_api_key", return_value=(None, 5)):
            data = await util._generate_file_data(
                "text", "md", "llm", api_user_id=5, default_caption="cap"
            )

        self.assertTrue(data.filename.startswith("message_"))
        self.assertEqual(data.caption, "cap")

    async def test_without_a_generator_the_default_model_writes_the_title(self):
        complete = mock.AsyncMock(
            return_value=util.FilenameGeneration(
                title="T", title_as_file_name="t", short_description="d"
            )
        )
        with mock.patch.object(
            util, "_resolve_title_api_key", return_value=("key", 5)
        ), mock.patch.object(title_util, "complete_structured", complete):
            data = await util._generate_file_data("text", "md", "llm", api_user_id=5)

        self.assertEqual(data.filename, "t.md")
        self.assertEqual(complete.await_args.kwargs["model"], util.CHAT_TITLE_MODEL)
        self.assertEqual(complete.await_args.kwargs["api_key"], "key")


if __name__ == "__main__":
    unittest.main()
