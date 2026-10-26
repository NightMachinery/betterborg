"""Finding, caching and renewing the newest Vertex AI Flash and Flash Lite."""

import asyncio
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import httpx

from uniborg import gemini_providers, vertex_latest
from uniborg.constants import GEMINI_FLASH_LATEST, GEMINI_FLASH_LITE_LATEST

FLASH_BASELINE = gemini_providers.VERTEX_FLASH_BASELINE
LITE_BASELINE = gemini_providers.VERTEX_FLASH_LITE_BASELINE
BASELINES = {
    GEMINI_FLASH_LATEST: FLASH_BASELINE,
    GEMINI_FLASH_LITE_LATEST: LITE_BASELINE,
}
T0 = datetime(2026, 10, 26, 12, tzinfo=timezone.utc)


def _client(served=(), *, failing=()):
    """A client on which Vertex serves SERVED and errors on FAILING."""
    seen = []

    def handle(request):
        model = request.url.path.rsplit("/", 1)[1].removesuffix(":countTokens")
        seen.append(model)
        if f"gemini/{model}" in failing:
            return httpx.Response(429)
        if f"gemini/{model}" in served:
            return httpx.Response(200, json={"totalTokens": 1})
        return httpx.Response(404)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
    client.seen = seen
    return client


def _refresh(client, **kwargs):
    return asyncio.run(vertex_latest.refresh("synthetic-key", client=client, **kwargs))


class _Stop(Exception):
    pass


class _Restore(unittest.TestCase):
    def setUp(self):
        saved = dict(gemini_providers.VERTEX_LATEST_IDS)
        self.addCleanup(gemini_providers.VERTEX_LATEST_IDS.update, saved)


class CandidateTests(unittest.TestCase):
    def test_versions_are_read_only_from_their_own_family(self):
        self.assertEqual(
            vertex_latest.version_of(FLASH_BASELINE, suffix="flash"), (3, 8, 1)
        )
        self.assertEqual(
            vertex_latest.version_of("gemini-4-flash", suffix="flash"), (4, 0, 1)
        )
        self.assertEqual(
            vertex_latest.version_of("gemini/gemini-3-flash-preview", suffix="flash"),
            (3, 0, 0),
        )
        self.assertIsNone(vertex_latest.version_of(LITE_BASELINE, suffix="flash"))
        self.assertIsNone(
            vertex_latest.version_of("gemini/gemini-3-flash-image", suffix="flash")
        )

    def test_candidates_run_through_the_next_major_version(self):
        ids = vertex_latest.candidates_after((3, 8, 1), suffix="flash")
        self.assertEqual(
            ids[:6],
            [
                "gemini/gemini-3.9-flash",
                "gemini/gemini-3.9-flash-preview",
                "gemini/gemini-4-flash",
                "gemini/gemini-4-flash-preview",
                "gemini/gemini-4.0-flash",
                "gemini/gemini-4.0-flash-preview",
            ],
        )
        self.assertEqual(ids[-1], "gemini/gemini-4.9-flash-preview")

    def test_after_a_preview_comes_its_release(self):
        ids = vertex_latest.candidates_after((4, 0, 0), suffix="flash")
        self.assertEqual(ids[:2], ["gemini/gemini-4-flash", "gemini/gemini-4.0-flash"])
        self.assertNotIn("gemini/gemini-4-flash-preview", ids)


class RefreshTests(unittest.TestCase):
    def test_the_newest_served_model_of_each_family_wins(self):
        client = _client(
            {
                "gemini/gemini-3.9-flash",
                "gemini/gemini-4.1-flash",
                "gemini/gemini-3.6-flash-lite",
            }
        )
        result = _refresh(client)
        self.assertTrue(result.complete)
        self.assertEqual(
            result.ids,
            {
                GEMINI_FLASH_LATEST: "gemini/gemini-4.1-flash",
                GEMINI_FLASH_LITE_LATEST: "gemini/gemini-3.6-flash-lite",
            },
        )
        self.assertNotIn("gemini-3.8-flash", client.seen)

    def test_a_newer_preview_wins_until_its_release_ships(self):
        result = _refresh(_client({"gemini/gemini-4-flash-preview"}))
        self.assertEqual(
            result.ids[GEMINI_FLASH_LATEST], "gemini/gemini-4-flash-preview"
        )
        result = _refresh(
            _client({"gemini/gemini-4-flash-preview", "gemini/gemini-4-flash"})
        )
        self.assertEqual(result.ids[GEMINI_FLASH_LATEST], "gemini/gemini-4-flash")

    def test_nothing_newer_keeps_the_baselines(self):
        self.assertEqual(_refresh(_client()).ids, BASELINES)

    def test_a_withdrawn_model_falls_back_to_the_newest_still_served(self):
        result = _refresh(
            _client({"gemini/gemini-3.9-flash"}),
            current={GEMINI_FLASH_LATEST: "gemini/gemini-4.2-flash"},
        )
        self.assertEqual(result.ids[GEMINI_FLASH_LATEST], "gemini/gemini-3.9-flash")

    def test_probing_continues_past_the_current_model(self):
        result = _refresh(
            _client({"gemini/gemini-5.1-flash"}),
            current={GEMINI_FLASH_LATEST: "gemini/gemini-4.2-flash"},
        )
        self.assertEqual(result.ids[GEMINI_FLASH_LATEST], "gemini/gemini-5.1-flash")

    def test_a_failed_probe_makes_the_result_incomplete(self):
        result = _refresh(_client(failing={"gemini/gemini-4-flash"}))
        self.assertFalse(result.complete)


class MappingTests(_Restore):
    def test_applied_models_are_what_the_aliases_are_sent_as(self):
        vertex_latest.apply({GEMINI_FLASH_LATEST: "gemini/gemini-3.9-flash"})
        self.assertEqual(
            gemini_providers.vertex_model_id(GEMINI_FLASH_LATEST),
            "gemini/gemini-3.9-flash",
        )
        self.assertEqual(
            gemini_providers.vertex_model_id("gemini-flash-latest"), "gemini-3.9-flash"
        )
        self.assertEqual(
            gemini_providers.vertex_model_id(GEMINI_FLASH_LITE_LATEST), LITE_BASELINE
        )


class KeepFreshTests(_Restore):
    def setUp(self):
        super().setUp()
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = os.path.join(directory.name, "latest.json")
        self.clock = T0
        self.sleeps = []
        self.refresher = AsyncMock(
            return_value=vertex_latest.RefreshResult(
                {**BASELINES, GEMINI_FLASH_LATEST: "gemini/gemini-3.9-flash"}, True
            )
        )

    async def _sleep(self, seconds):
        self.sleeps.append(seconds)
        if len(self.sleeps) >= self.max_sleeps:
            raise _Stop()
        self.clock += timedelta(seconds=seconds)

    def run_loop(self, *, max_sleeps=1, key="synthetic-key"):
        self.max_sleeps = max_sleeps
        with self.assertRaises(_Stop):
            asyncio.run(
                vertex_latest.keep_fresh(
                    api_key=lambda: key,
                    cache_path=self.path,
                    now=lambda: self.clock,
                    sleep=self._sleep,
                    refresher=self.refresher,
                )
            )

    def test_a_missing_cache_is_probed_saved_and_renewed_when_it_expires(self):
        self.run_loop(max_sleeps=2)
        self.assertEqual(self.refresher.await_count, 2)
        self.assertEqual(self.sleeps, [86400, 86400])
        self.assertEqual(
            gemini_providers.VERTEX_LATEST_IDS[GEMINI_FLASH_LATEST],
            "gemini/gemini-3.9-flash",
        )
        cache = vertex_latest.load_cache(self.path)
        self.assertEqual(cache.checked_at, T0 + timedelta(days=1))

    def test_a_fresh_cache_is_applied_without_probing(self):
        ids = {**BASELINES, GEMINI_FLASH_LATEST: "gemini/gemini-4-flash"}
        vertex_latest.save_cache(
            vertex_latest.Cache(T0 - timedelta(hours=20), ids), self.path
        )
        self.run_loop()
        self.refresher.assert_not_awaited()
        self.assertEqual(self.sleeps, [4 * 3600])
        self.assertEqual(
            gemini_providers.VERTEX_LATEST_IDS[GEMINI_FLASH_LATEST],
            "gemini/gemini-4-flash",
        )

    def test_a_cache_renewed_by_another_process_is_used(self):
        vertex_latest.save_cache(
            vertex_latest.Cache(T0 - timedelta(hours=20), BASELINES), self.path
        )
        original = self._sleep

        async def sleep(seconds):
            vertex_latest.save_cache(vertex_latest.Cache(T0, BASELINES), self.path)
            await original(seconds)

        self._sleep = sleep
        self.run_loop(max_sleeps=2)
        self.refresher.assert_not_awaited()
        self.assertEqual(self.sleeps, [4 * 3600, 20 * 3600])

    def test_an_incomplete_probe_is_applied_but_not_cached(self):
        self.refresher.return_value = vertex_latest.RefreshResult(
            {**BASELINES, GEMINI_FLASH_LATEST: "gemini/gemini-3.9-flash"}, False
        )
        self.run_loop()
        self.assertEqual(self.sleeps, [3600])
        self.assertIsNone(vertex_latest.load_cache(self.path))
        self.assertEqual(
            gemini_providers.VERTEX_LATEST_IDS[GEMINI_FLASH_LATEST],
            "gemini/gemini-3.9-flash",
        )

    def test_without_a_key_it_waits_and_tries_again(self):
        with patch.object(vertex_latest._log, "warning"):
            self.run_loop(key=None)
        self.refresher.assert_not_awaited()
        self.assertEqual(self.sleeps, [3600])


if __name__ == "__main__":
    unittest.main()
