import asyncio
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import openai

from uniborg import openai_providers as op
from uniborg.constants import (
    GPT_ASTRA,
    GPT_SOL,
    OPENAI_CODEX_ASTRA,
    OPENAI_CODEX_SOL,
    OR_OPENAI_ASTRA,
)


def route(provider, **kwargs):
    facts = dict(
        codex_access=True, openai_any=True, openai_usable=True, codex_exhausted=False
    )
    facts.update(kwargs)
    return op.choose_backend(provider, **facts)


class ChooseBackendTests(unittest.TestCase):
    def test_fixed_providers_ignore_quota(self):
        self.assertEqual(route(op.CODEX, codex_exhausted=True).backend, op.CODEX)
        self.assertEqual(route(op.OPENROUTER, openai_any=False).backend, op.OPENROUTER)
        self.assertEqual(route(op.OPENAI, openai_usable=False).backend, op.OPENAI)

    def test_openai_without_a_key_uses_codex_when_allowed(self):
        chosen = route(op.OPENAI, openai_any=False, openai_usable=False)
        self.assertEqual(chosen.backend, op.CODEX)
        self.assertEqual(chosen.note, op.NO_KEY_NOTE)

    def test_openai_without_a_key_or_codex_asks_for_a_key(self):
        chosen = route(
            op.OPENAI, openai_any=False, openai_usable=False, codex_access=False
        )
        self.assertEqual(chosen, op.Route(op.OPENAI))

    def test_auto_openai_first(self):
        self.assertEqual(
            route(op.AUTO_OPENAI_FIRST), op.Route(op.OPENAI, fallback=op.CODEX)
        )
        spent = route(op.AUTO_OPENAI_FIRST, openai_usable=False)
        self.assertEqual(spent.backend, op.CODEX)
        self.assertEqual(spent.note, op.OPENAI_SPENT_NOTE)
        #: Both spent: try the key anyway, Codex may still catch it.
        both = route(op.AUTO_OPENAI_FIRST, openai_usable=False, codex_exhausted=True)
        self.assertEqual(both, op.Route(op.OPENAI, fallback=op.CODEX))
        no_codex = route(op.AUTO_OPENAI_FIRST, codex_access=False)
        self.assertEqual(no_codex, op.Route(op.OPENAI))

    def test_auto_codex_first(self):
        self.assertEqual(
            route(op.AUTO_CODEX_FIRST), op.Route(op.CODEX, fallback=op.OPENAI)
        )
        spent = route(op.AUTO_CODEX_FIRST, codex_exhausted=True)
        self.assertEqual(spent.backend, op.OPENAI)
        self.assertEqual(spent.note, op.CODEX_SPENT_NOTE)
        no_key = route(op.AUTO_CODEX_FIRST, openai_any=False, openai_usable=False)
        self.assertEqual(no_key, op.Route(op.CODEX))
        no_codex = route(op.AUTO_CODEX_FIRST, codex_access=False)
        self.assertEqual(no_codex.backend, op.OPENAI)
        self.assertIsNone(no_codex.note)

    def test_unknown_provider_raises(self):
        with self.assertRaises(ValueError):
            route("gpt-everywhere")


class ModelIdTests(unittest.TestCase):
    def test_backend_models(self):
        self.assertEqual(op.backend_model(GPT_SOL, op.CODEX), OPENAI_CODEX_SOL)
        self.assertEqual(
            op.backend_model(GPT_SOL, op.OPENAI), "openai-api/gpt-sol-latest"
        )
        self.assertEqual(op.backend_model(GPT_ASTRA, op.OPENROUTER), OR_OPENAI_ASTRA)
        with self.assertRaises(ValueError):
            op.backend_model(GPT_SOL, "nowhere")

    def test_openai_api_slug_follows_the_codex_alias(self):
        self.assertEqual(
            op.openai_api_slug("openai-api/gpt-astra-latest"), "gpt-6-astra"
        )
        self.assertEqual(op.openai_api_slug("openai-api/gpt-audio"), "gpt-audio")

    def test_neutral_for_codex(self):
        self.assertEqual(op.neutral_for_codex(OPENAI_CODEX_ASTRA), GPT_ASTRA)
        self.assertIsNone(op.neutral_for_codex("openai-codex/gpt-reserve"))

    def test_openrouter_levels_are_clamped(self):
        self.assertEqual(op.openrouter_level("max"), "high")
        self.assertEqual(op.openrouter_level("none"), "low")
        self.assertEqual(op.openrouter_level("medium"), "medium")
        self.assertIsNone(op.openrouter_level(None))

    def test_normalize(self):
        self.assertEqual(op.normalize(None), op.DEFAULT)
        self.assertEqual(op.normalize("bogus"), op.DEFAULT)
        self.assertEqual(op.normalize(op.AUTO_CODEX_FIRST), op.AUTO_CODEX_FIRST)


class _FakeRedis:
    def __init__(self):
        self.data = {}
        self.ttl = {}

    async def set(self, name, value, ex=None):
        self.data[name] = value
        self.ttl[name] = ex

    async def get(self, name):
        return self.data.get(name)

    async def delete(self, name):
        self.data.pop(name, None)


class QuotaMemoryTests(unittest.TestCase):
    def setUp(self):
        self.now = 1_000_000.0
        self.redis = _FakeRedis()

        async def get_redis():
            return self.redis

        self.memory = op.QuotaMemory(get_redis=get_redis, clock=lambda: self.now)

    def test_a_spent_key_is_skipped_for_72_hours_without_storing_the_key(self):
        asyncio.run(self.memory.mark_key_spent("sk-secret"))
        self.assertTrue(asyncio.run(self.memory.key_spent_until("sk-secret")))
        self.assertFalse(asyncio.run(self.memory.key_spent_until("sk-other")))
        (name,) = self.redis.data
        self.assertNotIn("sk-secret", name)
        self.assertEqual(self.redis.ttl[name], 72 * 3600)
        self.now += 72 * 3600 + 1
        self.assertIsNone(asyncio.run(self.memory.key_spent_until("sk-secret")))

    def test_clearing_a_key_retries_it(self):
        asyncio.run(self.memory.mark_key_spent("sk-secret"))
        asyncio.run(self.memory.clear_key("sk-secret"))
        self.assertIsNone(asyncio.run(self.memory.key_spent_until("sk-secret")))

    def test_codex_is_spent_until_its_reset(self):
        reset = datetime.fromtimestamp(self.now + 600, tz=timezone.utc)
        asyncio.run(self.memory.mark_codex_spent(reset))
        self.assertEqual(asyncio.run(self.memory.codex_spent_until()), reset)
        self.now += 601
        self.assertIsNone(asyncio.run(self.memory.codex_spent_until()))

    def test_a_reset_in_the_past_is_ignored(self):
        past = datetime.fromtimestamp(self.now - 1, tz=timezone.utc)
        asyncio.run(self.memory.mark_codex_spent(past))
        self.assertIsNone(asyncio.run(self.memory.codex_spent_until()))

    def test_without_redis_it_remembers_in_memory(self):
        memory = op.QuotaMemory(clock=lambda: self.now)
        asyncio.run(memory.mark_codex_spent(None))
        until = asyncio.run(memory.codex_spent_until())
        self.assertEqual(
            until - datetime.fromtimestamp(self.now, tz=timezone.utc),
            timedelta(hours=1),
        )


class InsufficientQuotaTests(unittest.TestCase):
    def _sdk_error(self, code):
        response = SimpleNamespace(
            request=None, status_code=429, headers={}, text="", json=lambda: {}
        )
        error = openai.RateLimitError.__new__(openai.RateLimitError)
        Exception.__init__(error, "Error code: 429")
        error.body = {"error": {"code": code, "message": "..."}}
        error.code = code
        error.response = response
        return error

    def test_detects_insufficient_quota_through_a_wrapper(self):
        try:
            try:
                raise self._sdk_error("insufficient_quota")
            except Exception as exc:
                raise RuntimeError("Codex stream failed") from exc
        except RuntimeError as wrapped:
            self.assertTrue(op.is_insufficient_quota(wrapped))

    def test_a_plain_rate_limit_is_not_out_of_credit(self):
        self.assertFalse(
            op.is_insufficient_quota(self._sdk_error("rate_limit_exceeded"))
        )
        self.assertFalse(op.is_insufficient_quota(RuntimeError("timeout")))


if __name__ == "__main__":
    unittest.main()
