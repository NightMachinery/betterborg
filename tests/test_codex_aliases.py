"""Codex model aliases resolved from the catalog (`uniborg/codex_aliases.py`).

The catalog below is the real one of 2026-10-01, trimmed to the fields the
resolver reads. Redis is a dict, so "another process" is fresh module state
over the same dict.
"""

import asyncio
import copy
import unittest
from unittest.mock import patch

from uniborg import codex_aliases, codex_util, llm_models
from uniborg.constants import OPENAI_CODEX_ASTRA, OPENAI_CODEX_LUNA, OPENAI_CODEX_SOL

_WITH_ULTRA = ["low", "medium", "high", "xhigh", "max", "ultra"]
_NO_ULTRA = ["low", "medium", "high", "xhigh", "max"]


def _entry(slug, priority, levels, *, visibility="list", api=True):
    return {
        "slug": slug,
        "priority": priority,
        "visibility": visibility,
        "supported_in_api": api,
        "supported_reasoning_levels": [{"effort": level} for level in levels],
    }


CATALOG = [
    _entry("gpt-6.1-sol", 1, _WITH_ULTRA),
    _entry("gpt-6-astra", 2, _WITH_ULTRA),
    _entry("gpt-6-sol", 3, _WITH_ULTRA),
    _entry("gpt-6-luna", 4, _NO_ULTRA),
    _entry("gpt-reserve", 4, _NO_ULTRA, visibility="hide"),
    _entry("gpt-5.6-sol", 5, _WITH_ULTRA),
    _entry("gpt-5.6-terra", 8, _WITH_ULTRA),
    _entry("gpt-5.6-luna", 9, _NO_ULTRA),
    _entry("gpt-5.5", 13, ["low", "medium", "high", "xhigh"]),
    _entry("codex-auto-review", 43, _NO_ULTRA, visibility="hide"),
]


class CodexAliasTests(unittest.TestCase):
    def setUp(self):
        self.redis = {}
        self.catalog = copy.deepcopy(CATALOG)
        self.notices = []
        self.forget_memory()

        async def get(key, **kwargs):
            return self.redis.get(key)

        async def set_(key, value, **kwargs):
            self.redis[key] = value
            return True

        for name, value in (("get_and_renew", get), ("set_with_expiry", set_)):
            patcher = patch.object(codex_aliases.redis_util, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.addCleanup(self.forget_memory)

    def forget_memory(self):
        """What a freshly started process knows."""
        codex_aliases._resolved.clear()
        codex_aliases._missing.clear()

    def refresh(self):
        async def fetch():
            return copy.deepcopy(self.catalog)

        async def notify(text):
            self.notices.append(text)

        return asyncio.run(codex_aliases.refresh(fetch_catalog=fetch, notify=notify))

    def test_todays_catalog_matches_the_pinned_models_so_nothing_is_said(self):
        self.assertEqual(self.refresh(), [])
        self.assertEqual(self.notices, [])
        self.assertEqual(codex_util.codex_model_name(OPENAI_CODEX_SOL), "gpt-6.1-sol")
        self.assertEqual(codex_util.codex_model_name(OPENAI_CODEX_LUNA), "gpt-6-luna")

    def test_a_newer_model_moves_its_alias_and_the_admins_hear_once(self):
        self.catalog.append(_entry("gpt-6.2-sol", 1, _WITH_ULTRA))

        self.refresh()
        self.refresh()

        (notice,) = self.notices
        self.assertIn("`gpt-6.2-sol` (GPT-6.2 Sol), was `gpt-6.1-sol`", notice)
        self.assertEqual(codex_util.codex_model_name(OPENAI_CODEX_SOL), "gpt-6.2-sol")
        self.assertEqual(
            llm_models.codex_model_choices()[OPENAI_CODEX_SOL], "GPT-6.2 Sol (Codex)"
        )
        self.assertEqual(codex_util.codex_model_name(OPENAI_CODEX_ASTRA), "gpt-6-astra")

    def test_hidden_and_api_less_models_do_not_count(self):
        self.catalog += [
            _entry("gpt-7-sol", 1, _WITH_ULTRA, visibility="hide"),
            _entry("gpt-7-astra", 1, _WITH_ULTRA, api=False),
        ]

        self.assertEqual(self.refresh(), [])

    def test_versions_compare_as_numbers(self):
        self.catalog.append(_entry("gpt-6.10-sol", 9, _WITH_ULTRA))

        self.refresh()

        self.assertEqual(codex_util.codex_model_name(OPENAI_CODEX_SOL), "gpt-6.10-sol")

    def test_levels_drop_ultra_and_offer_none_only_where_probed(self):
        self.catalog += [
            _entry("gpt-7-sol", 1, ["medium", "high", "ultra"]),
            _entry("gpt-7-luna", 1, ["low", "medium"]),
        ]

        self.refresh()

        self.assertEqual(
            llm_models.reasoning_levels_for_model(OPENAI_CODEX_SOL),
            ("medium", "high"),
        )
        self.assertEqual(
            llm_models.reasoning_levels_for_model(OPENAI_CODEX_LUNA),
            ("none", "low", "medium"),
        )
        self.assertIn("Reasoning levels: medium, high", self.notices[0])

    def test_a_change_another_process_announced_is_not_announced_again(self):
        self.catalog.append(_entry("gpt-6.2-sol", 1, _WITH_ULTRA))
        self.refresh()
        self.forget_memory()

        self.assertEqual(self.refresh(), [])
        self.assertEqual(len(self.notices), 1)
        self.assertEqual(codex_util.codex_model_name(OPENAI_CODEX_SOL), "gpt-6.2-sol")

    def test_a_family_gone_from_the_catalog_keeps_its_model_and_is_said_once(self):
        astra = [entry for entry in self.catalog if entry["slug"] == "gpt-6-astra"]
        self.catalog.remove(astra[0])

        self.refresh()
        self.refresh()
        self.assertEqual(len(self.notices), 1)
        self.assertIn("no longer offers", self.notices[0])
        self.assertEqual(codex_util.codex_model_name(OPENAI_CODEX_ASTRA), "gpt-6-astra")

        self.catalog += astra
        self.refresh()
        self.catalog.remove(astra[0])
        self.refresh()
        self.assertEqual(len(self.notices), 2)

    def test_a_failing_catalog_is_retried_and_on_change_runs_after_a_change(self):
        calls = []
        changes = []
        changed = asyncio.Event()

        def on_change():
            changes.append(None)
            changed.set()

        async def fetch():
            calls.append(None)
            if len(calls) == 1:
                raise RuntimeError("catalog down")
            return CATALOG + [_entry("gpt-6.2-sol", 1, _WITH_ULTRA)]

        async def run():
            with self.assertLogs(codex_aliases._log, "WARNING"):
                task = asyncio.ensure_future(
                    codex_aliases.refresh_forever(
                        interval_seconds=0, fetch_catalog=fetch, on_change=on_change
                    )
                )
                await asyncio.wait_for(changed.wait(), 1)
                await asyncio.sleep(0.01)
                task.cancel()

        asyncio.run(run())

        #: Later rounds find nothing new.
        self.assertGreater(len(calls), 2)
        self.assertEqual(len(changes), 1)

    def test_plain_codex_ids_pass_through(self):
        self.assertEqual(
            codex_util.codex_model_name("openai-codex/gpt-reserve"), "gpt-reserve"
        )
        self.assertIsNone(codex_aliases.resolution_for("openai-codex/gpt-reserve"))


if __name__ == "__main__":
    unittest.main()
