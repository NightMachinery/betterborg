import asyncio
import builtins
import importlib
import unittest
from types import SimpleNamespace

from uniborg import codex_util, llm_models
from uniborg.constants import (
    OPENAI_CODEX_ASTRA,
    OPENAI_CODEX_GPT_5_6_LUNA,
    OPENAI_CODEX_GPT_5_6_SOL,
    OPENAI_CODEX_LUNA_RESERVE,
)


class _FakeLoop:
    def create_task(self, coro):
        coro.close()


builtins.borg = SimpleNamespace(loop=_FakeLoop())


async def _import_plugin():
    return importlib.import_module("llm_chat_plugins.llm_chat")


plugin = asyncio.run(_import_plugin())


class LunaReserveModelTests(unittest.TestCase):
    def test_reserve_slug_is_gpt_reserve_not_the_display_model(self):
        #: The whole point: `gpt-5.6-luna` bills to the regular allowance, only
        #: `gpt-reserve` reaches the separate reserve meter.
        self.assertEqual(OPENAI_CODEX_LUNA_RESERVE, "openai-codex/gpt-reserve")
        self.assertNotEqual(OPENAI_CODEX_LUNA_RESERVE, OPENAI_CODEX_GPT_5_6_LUNA)

    def test_is_luna_reserve_model(self):
        self.assertTrue(codex_util.is_luna_reserve_model(OPENAI_CODEX_LUNA_RESERVE))
        self.assertTrue(codex_util.is_luna_reserve_model("gpt-reserve"))
        for other in (
            OPENAI_CODEX_GPT_5_6_LUNA,
            OPENAI_CODEX_GPT_5_6_SOL,
            OPENAI_CODEX_ASTRA,
            "",
            None,
        ):
            with self.subTest(model=other):
                self.assertFalse(codex_util.is_luna_reserve_model(other))

    def test_reserve_is_a_codex_model(self):
        self.assertTrue(codex_util.is_codex_model(OPENAI_CODEX_LUNA_RESERVE))
        self.assertEqual(
            codex_util.codex_model_name(OPENAI_CODEX_LUNA_RESERVE), "gpt-reserve"
        )

    def test_registry_offers_the_reserve_to_codex_users(self):
        spec = llm_models.spec_for_model(OPENAI_CODEX_LUNA_RESERVE)
        self.assertEqual(spec.display_name, "Luna Reserve (Codex)")
        self.assertTrue(spec.codex_access)
        self.assertFalse(spec.hidden)
        self.assertFalse(spec.admin_only)
        self.assertIn(OPENAI_CODEX_LUNA_RESERVE, llm_models.codex_model_choices())

    def test_reserve_supports_every_openai_reasoning_level(self):
        #: Verified live against the backend: none/low/medium/high/xhigh/max.
        spec = llm_models.spec_for_model(OPENAI_CODEX_LUNA_RESERVE)
        self.assertEqual(spec.reasoning_levels, llm_models.OPENAI_REASONING_LEVELS)


class LunaReservePrefixTests(unittest.TestCase):
    def detect(self, text, *, codex_p=True):
        return plugin._detect_and_process_message_prefix(text, codex_p=codex_p)

    def test_cr_selects_the_reserve(self):
        result = self.detect(".cr hello")
        self.assertEqual(result.model, OPENAI_CODEX_LUNA_RESERVE)
        self.assertEqual(result.reasoning_effort, "medium")
        self.assertEqual(result.processed_text, "hello")

    def test_persian_alias_selects_the_reserve(self):
        result = self.detect(".چر سلام")
        self.assertEqual(result.model, OPENAI_CODEX_LUNA_RESERVE)
        self.assertEqual(result.processed_text, "سلام")

    def test_cr_does_not_shadow_the_shorter_c_prefix(self):
        #: Longest-match wins, so `.c` must still mean Sol.
        self.assertEqual(self.detect(".c hello").model, OPENAI_CODEX_GPT_5_6_SOL)

    def test_cr_combines_with_an_effort_prefix(self):
        result = self.detect(".cr .th hello")
        self.assertEqual(result.model, OPENAI_CODEX_LUNA_RESERVE)
        self.assertEqual(result.reasoning_effort, "high")

    def test_cr_is_recognized_but_restricted_without_codex_access(self):
        #: Restricted prefixes must not leak into the prompt text.
        result = self.detect(".cr hello", codex_p=False)
        self.assertEqual(result.model, OPENAI_CODEX_LUNA_RESERVE)
        self.assertIn((".cr", ".چر"), plugin.DENIED_CODEX_PREFIX_MODEL_MAPPING)


if __name__ == "__main__":
    unittest.main()
