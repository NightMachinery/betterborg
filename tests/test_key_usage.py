"""The usage ledger: what is recorded, priced and shown."""

import asyncio
import builtins
import importlib
import itertools
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

from uniborg import codex_util, key_sharing, key_usage

_ids = itertools.count(881_000_000)
NOW = datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc)
SOL = "openai-api/gpt-sol-latest"


def _user() -> int:
    return next(_ids)


class _FakeLoop:
    def create_task(self, coro):
        coro.close()


builtins.borg = SimpleNamespace(loop=_FakeLoop())


async def _import_plugin():
    return importlib.import_module("llm_chat_plugins.llm_chat")


plugin = asyncio.run(_import_plugin())


class LedgerTests(unittest.TestCase):
    def setUp(self):
        self.owner, self.user = _user(), _user()

    def record(self, tokens=None, *, clock=lambda: NOW, **overrides):
        fields = dict(
            payer_id=self.owner,
            user_id=self.user,
            backend="openai",
            model=SOL,
            tokens=tokens,
            clock=clock,
        )
        fields.update(overrides)
        key_usage.record(**fields)

    def test_requests_add_up_per_user_and_backend(self):
        self.record(key_usage.Tokens(1_000_000, 0, 100_000))
        self.record(key_usage.Tokens(1_000_000, 0, 100_000))
        totals = key_usage.by_user(payer_id=self.owner, clock=lambda: NOW)
        total = totals[(self.user, "openai")]
        self.assertEqual(total.requests, 2)
        self.assertAlmostEqual(total.cost_usd, 2 * (2.0 + 1.0))
        self.assertFalse(total.partial)

    def test_cached_input_is_cheaper(self):
        tokens = key_usage.Tokens(1_000_000, 1_000_000, 0)
        self.assertAlmostEqual(key_usage.estimate_cost(SOL, tokens), 0.2)

    def test_unmetered_requests_mark_the_cost_partial(self):
        self.record(key_usage.Tokens(1000, 0, 1000))
        self.record(None)
        total = key_usage.by_user(payer_id=self.owner, clock=lambda: NOW)[
            (self.user, "openai")
        ]
        self.assertTrue(total.partial)
        self.assertTrue(key_usage.describe(total).endswith("+"))

    def test_the_payers_own_use_and_old_days_are_left_out(self):
        self.record(user_id=self.owner)
        self.record(clock=lambda: NOW - timedelta(days=40))
        self.assertEqual(key_usage.by_user(payer_id=self.owner, clock=lambda: NOW), {})

    def test_of_user_splits_by_payer(self):
        self.record()
        self.record(payer_id=key_usage.CODEX_PAYER, backend="codex")
        totals = key_usage.of_user(self.user, clock=lambda: NOW)
        self.assertEqual(
            set(totals), {(self.owner, "openai"), (key_usage.CODEX_PAYER, "codex")}
        )

    def test_prices_match_the_family_in_any_backend_id(self):
        for model in (
            SOL,
            "openai-codex/gpt-sol-latest",
            "openrouter/~openai/gpt-sol-latest",
        ):
            self.assertIs(key_usage.price_for(model), key_usage.PRICES["sol"])
        self.assertIsNone(key_usage.price_for("gemini/gemini-flash-latest"))

    def test_describe(self):
        total = key_usage.UsageTotal(
            requests=12, input_tokens=300_000, output_tokens=40_000, cost_usd=0.42
        )
        self.assertEqual(key_usage.describe(total), "12 requests, 340k tokens, ~$0.42")


class RecordUsageTests(unittest.TestCase):
    def setUp(self):
        self.user = _user()

    def generation(self, usage=None):
        return plugin.GenerationResult(
            text="hi", finish_reason="stop", has_image=False, usage=usage
        )

    def recorded(self, **kwargs):
        with patch.object(plugin.key_usage, "record") as record:
            plugin._record_usage(self.user, **kwargs)
        return record

    def test_codex_requests_are_paid_by_codex(self):
        record = self.recorded(
            model="openai-codex/gpt-sol-latest",
            service="codex",
            credential=None,
            generation=self.generation(codex_util.TokenUsage(10, 2, 5)),
        )
        kwargs = record.call_args.kwargs
        self.assertEqual(kwargs["payer_id"], key_usage.CODEX_PAYER)
        self.assertEqual(kwargs["tokens"], key_usage.Tokens(10, 2, 5))

    def test_a_shared_key_is_paid_by_its_owner(self):
        credential = key_sharing.Credential("sk-x", "share:7", key_sharing.SHARE, 7)
        record = self.recorded(
            model=SOL,
            service="openai",
            credential=credential,
            generation=self.generation(),
        )
        self.assertEqual(record.call_args.kwargs["payer_id"], 7)
        self.assertIsNone(record.call_args.kwargs["tokens"])

    def test_the_users_own_key_is_not_recorded(self):
        credential = key_sharing.Credential("sk-x", "own", key_sharing.OWN, self.user)
        record = self.recorded(
            model=SOL,
            service="openai",
            credential=credential,
            generation=self.generation(),
        )
        record.assert_not_called()

    def test_a_ledger_failure_does_not_reach_the_user(self):
        with patch.object(
            plugin.key_usage, "record", side_effect=RuntimeError("locked")
        ), patch.object(plugin, "logger", create=True):
            plugin._record_usage(
                self.user,
                model="openai-codex/gpt-sol-latest",
                service="codex",
                credential=None,
                generation=self.generation(),
            )


class PanelTests(unittest.TestCase):
    def test_sharers_and_admins_see_usage(self):
        owner, user = _user(), _user()
        key_usage.record(
            payer_id=owner,
            user_id=user,
            backend="openai",
            model=SOL,
            tokens=key_usage.Tokens(1000, 0, 1000),
        )
        key_usage.record(
            payer_id=key_usage.CODEX_PAYER,
            user_id=user,
            backend="codex",
            model="openai-codex/gpt-sol-latest",
        )
        with patch.object(
            plugin, "_user_display_name", side_effect=lambda uid: f"U{uid}"
        ):
            lines = plugin._key_usage_lines(owner)
            admin_text = plugin._codex_user_usage_text(user)
        self.assertEqual(len(lines), 1)
        self.assertIn(f"U{user} ({user}): OpenAI 1 request", lines[0])
        self.assertIn("• Codex: 1 request", admin_text)
        self.assertIn(f"OpenAI, key of U{owner}: 1 request", admin_text)


if __name__ == "__main__":
    unittest.main()
