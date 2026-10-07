"""Provider switching, private key setup, job isolation and Vertex request shape."""

import asyncio
import base64
import builtins
import importlib
import json
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import httpx

from uniborg import stt_models, stt_providers, tg_compat


class _Loop:
    def create_task(self, coro):
        coro.close()


class _Borg:
    loop = _Loop()

    def on(self, *args, **kwargs):
        return lambda func: func


previous = getattr(builtins, "borg", None)
builtins.borg = _Borg()
try:

    async def _import():
        return importlib.import_module("stt_plugins.stt")

    stt = asyncio.run(_import())
finally:
    if previous is not None:
        builtins.borg = previous


class ProviderWorkflowTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.saved = {}
        self.keys = {
            "gemini": "synthetic-gemini-key-000000",
            "vertex": "synthetic-vertex-key-000000",
        }
        storage = SimpleNamespace(
            get=lambda uid: dict(self.saved.get(uid, {})),
            set=lambda uid, data: self.saved.__setitem__(uid, dict(data)) or True,
        )
        patches = (
            patch.object(stt, "_prefs_storage", storage),
            patch.object(
                stt,
                "get_effective_gemini_api_key",
                side_effect=lambda uid: self.keys.get("gemini"),
            ),
            patch.object(
                stt.llm_db,
                "get_api_key",
                side_effect=lambda **kw: self.keys.get(kw["service"]),
            ),
            patch.object(
                stt.llm_db,
                "set_api_key",
                side_effect=lambda **kw: self.keys.__setitem__(
                    kw["service"], kw["key"]
                ),
            ),
            patch.object(
                stt.llm_util, "get_proxy_config_or_error", return_value=(None, None)
            ),
        )
        for item in patches:
            item.start()
            self.addCleanup(item.stop)
        self.info_patch = patch.object(stt.llm_util, "send_info_message", AsyncMock())
        self.info = self.info_patch.start()
        self.addCleanup(self.info_patch.stop)
        self.validation_patch = patch.object(stt_providers, "validate_key", AsyncMock())
        self.validate = self.validation_patch.start()
        self.addCleanup(self.validation_patch.stop)
        stt._key_setups.clear()
        self.addCleanup(stt._key_setups.clear)
        self.addCleanup(stt.llm_db.cancel_key_flow, 7)

    def event(self, action=None, *, uid=7, private=True):
        return SimpleNamespace(
            sender_id=uid,
            is_private=private,
            data=(
                f"{stt.PROVIDER_CALLBACK_PREFIX}{uid}:{action}".encode()
                if action
                else b""
            ),
            answer=AsyncMock(),
            edit=AsyncMock(),
            delete=AsyncMock(),
            reply=AsyncMock(),
            pattern_match=SimpleNamespace(group=lambda index: None),
            text="",
        )

    async def test_legacy_model_is_preserved_and_models_are_remembered_per_provider(
        self,
    ):
        self.saved[7] = {"model": "gemini/gemini-3.5-transcribe"}
        self.assertEqual(stt.get_provider_choice(7), "gemini")
        self.assertEqual(stt.get_model_choice(7), "gemini/gemini-3.5-transcribe")
        stt.set_provider_choice(7, "vertex")
        self.assertEqual(stt.get_model_choice(7), "auto")
        stt.set_model_choice(7, "gemini/gemini-3.5-flash")
        stt.set_provider_choice(7, "gemini")
        self.assertEqual(stt.get_model_choice(7), "gemini/gemini-3.5-transcribe")
        stt.set_provider_choice(7, "vertex")
        self.assertEqual(stt.get_model_choice(7), "gemini/gemini-3.5-flash")

    async def test_saved_key_switches_immediately_without_revalidating(self):
        event = self.event("use:vertex")
        await stt.provider_callback_handler(event)
        self.assertEqual(stt.get_provider_choice(7), "vertex")
        self.validate.assert_not_awaited()
        event.edit.assert_awaited_once()

    async def test_missing_key_switches_only_after_validation_and_saving(self):
        self.keys.pop("vertex")
        event = self.event("use:vertex")
        await stt.provider_callback_handler(event)
        self.assertEqual(stt.get_provider_choice(7), "gemini")
        self.assertEqual(stt.llm_db.get_awaiting_service(7), "vertex")
        key = "synthetic-new-vertex-key-0000"
        await stt._submit_provider_key(event, "vertex", key)
        self.validate.assert_awaited_once_with("vertex", key)
        self.assertEqual(self.keys["vertex"], key)
        self.assertEqual(stt.get_provider_choice(7), "vertex")
        event.delete.assert_awaited_once()
        self.assertFalse(stt.llm_db.is_awaiting_key(7))

    async def test_key_update_preserves_provider_and_never_echoes_the_key(self):
        event = self.event("key:vertex")
        await stt.provider_callback_handler(event)
        key = "synthetic-replacement-key-0000"
        await stt._submit_provider_key(event, "vertex", key)
        self.assertEqual(stt.get_provider_choice(7), "gemini")
        self.assertEqual(self.keys["vertex"], key)
        self.assertNotIn(key, str(self.info.await_args_list))

    async def test_failed_validation_preserves_saved_key_and_provider(self):
        old = self.keys["vertex"]
        self.validate.side_effect = stt_providers.ProviderError("vertex", 403)
        event = self.event()
        await stt._start_key_setup(event, "vertex", switch=True)
        await stt._submit_provider_key(event, "vertex", "synthetic-new-vertex-key-0000")
        self.assertEqual(self.keys["vertex"], old)
        self.assertEqual(stt.get_provider_choice(7), "gemini")
        self.assertIn("unchanged", self.info.await_args.args[1])

    async def test_cancel_during_validation_cannot_save_or_switch_later(self):
        event = self.event()
        await stt._start_key_setup(event, "vertex", switch=True)
        old = self.keys["vertex"]

        async def cancel(*args):
            stt._cancel_key_setup(7)

        self.validate.side_effect = cancel
        await stt._submit_provider_key(event, "vertex", "synthetic-new-vertex-key-0000")
        self.assertEqual(self.keys["vertex"], old)
        self.assertEqual(stt.get_provider_choice(7), "gemini")

    async def test_stale_cancel_button_cannot_cancel_a_new_key_flow(self):
        event = self.event()
        await stt._start_key_setup(event, "vertex", switch=True)
        old_token = stt._key_setups[7].token
        await stt._start_key_setup(event, "gemini", switch=False)
        await stt.provider_callback_handler(self.event(f"cancel:{old_token}"))
        self.assertEqual(stt.llm_db.get_awaiting_service(7), "gemini")

    async def test_inline_key_command_saves_without_switching(self):
        event = self.event()
        event.pattern_match = SimpleNamespace(
            group=lambda index: "synthetic-new-vertex-key-0000"
        )
        await stt.set_vertex_key_handler(event)
        self.assertEqual(self.keys["vertex"], "synthetic-new-vertex-key-0000")
        self.assertEqual(stt.get_provider_choice(7), "gemini")

    async def test_group_key_is_deleted_and_never_validated_or_saved(self):
        event = self.event(private=False)
        event.pattern_match = SimpleNamespace(
            group=lambda index: "synthetic-new-vertex-key-0000"
        )
        await stt.set_vertex_key_handler(event)
        event.delete.assert_awaited_once()
        self.validate.assert_not_awaited()
        self.assertNotIn(7, stt._key_setups)

    async def test_panel_contains_only_status_not_key_values(self):
        await stt._show_provider_panel(self.event())
        rendered = str(self.info.await_args)
        self.assertIn("Google AI Studio", rendered)
        self.assertNotIn(self.keys["gemini"], rendered)
        self.assertNotIn(self.keys["vertex"], rendered)

    async def test_wrong_owner_cannot_switch(self):
        event = self.event("use:vertex")
        event.sender_id = 8
        await stt.provider_callback_handler(event)
        self.assertEqual(stt.get_provider_choice(8), "gemini")
        event.edit.assert_not_awaited()
        self.assertTrue(event.answer.await_args.kwargs["alert"])

    async def test_deletion_failure_is_reported_without_echoing_key(self):
        event = self.event()
        event.delete.side_effect = RuntimeError("forbidden")
        await stt._start_key_setup(event, "vertex", switch=False)
        await stt._submit_provider_key(event, "vertex", "synthetic-new-vertex-key-0000")
        self.assertIn("please delete it yourself", self.info.await_args.args[1])

    async def test_database_failure_does_not_expose_exception_details(self):
        event = self.event()
        await stt._start_key_setup(event, "vertex", switch=True)
        with patch.object(
            stt.llm_db,
            "set_api_key",
            side_effect=RuntimeError("secret database parameter"),
        ):
            await stt._submit_provider_key(
                event, "vertex", "synthetic-new-vertex-key-0000"
            )
        self.assertNotIn("secret database parameter", str(self.info.await_args_list))
        self.assertEqual(stt.get_provider_choice(7), "gemini")

    async def test_preference_write_failure_never_claims_provider_switched(self):
        event = self.event()
        await stt._start_key_setup(event, "vertex", switch=True)
        # UserStorage returns None on an unexpected write failure, False on timeout.
        for failed in (None, False):
            with self.subTest(result=failed), patch.object(
                stt._prefs_storage, "set", return_value=failed
            ):
                await stt._start_key_setup(event, "vertex", switch=True)
                await stt._submit_provider_key(
                    event, "vertex", "synthetic-new-vertex-key-0000"
                )
                message = self.info.await_args.args[1]
                self.assertIn("key was saved", message)
                self.assertIn("settings could not be saved", message)
                self.assertNotIn("Now using", message)
                self.assertEqual(stt.get_provider_choice(7), "gemini")

    async def test_proxy_restriction_does_not_save_or_switch_or_validate(self):
        event = self.event()
        await stt._start_key_setup(event, "vertex", switch=True)
        with patch.object(
            stt.llm_util,
            "get_proxy_config_or_error",
            side_effect=RuntimeError("blocked"),
        ):
            await stt._submit_provider_key(
                event, "vertex", "synthetic-new-vertex-key-0000"
            )
        self.validate.assert_not_awaited()
        self.assertEqual(self.keys["vertex"], "synthetic-vertex-key-000000")
        self.assertEqual(stt.get_provider_choice(7), "gemini")

    async def test_invalid_key_is_deleted_and_never_reaches_provider(self):
        event = self.event()
        await stt._start_key_setup(event, "vertex", switch=True)
        await stt._submit_provider_key(event, "vertex", "bad")
        event.delete.assert_awaited_once()
        self.validate.assert_not_awaited()
        self.assertEqual(stt.get_provider_choice(7), "gemini")

    async def test_validation_task_cancellation_is_propagated_without_saving(self):
        self.validate.side_effect = asyncio.CancelledError()
        event = self.event()
        await stt._start_key_setup(event, "vertex", switch=True)
        with self.assertRaises(asyncio.CancelledError):
            await stt._submit_provider_key(
                event, "vertex", "synthetic-new-vertex-key-0000"
            )
        self.assertEqual(self.keys["vertex"], "synthetic-vertex-key-000000")
        self.assertEqual(stt.get_provider_choice(7), "gemini")

    async def test_provider_setup_button_payloads_fit_telegram_limit(self):
        event = self.event(uid=9223372036854775807)
        self.addCleanup(stt.llm_db.cancel_key_flow, event.sender_id)
        await stt._start_key_setup(event, "gemini", switch=True)
        for row in self.info.await_args.kwargs["buttons"]:
            for button in row:
                data = tg_compat.button_data(button)
                if data:
                    self.assertLessEqual(len(data), 64)

    async def test_old_model_menu_cannot_change_the_new_providers_model(self):
        stt.set_provider_choice(7, "vertex")
        for payload in (b"sttmodel_gemini:2.5-flash", b"sttmodel_2.5-flash"):
            with self.subTest(payload=payload):
                event = self.event()
                event.data = payload
                await stt.model_callback_handler(event)
                self.assertEqual(stt.get_model_choice(7), "auto")
                self.assertTrue(event.answer.await_args.kwargs["alert"])

    async def test_vertex_job_keeps_its_provider_key_and_model_after_settings_change(
        self,
    ):
        stt.set_provider_choice(7, "vertex")
        stt.set_model_choice(7, "gemini/gemini-3.5-flash")
        with patch.object(
            stt.llm_util, "create_attachments_from_dir", return_value=[object()]
        ), patch.object(stt.stt_models, "load_media_model") as load:
            job = stt.prepare_stt_job("/tmp/synthetic", user_id=7)
        load.assert_not_called()
        stt.set_provider_choice(7, "gemini")
        self.keys["vertex"] = "another-synthetic-key-000000"
        with patch.object(
            stt_providers,
            "transcribe_vertex",
            AsyncMock(return_value='{"transcription":"hello"}'),
        ) as call, patch.object(
            stt.redis_util, "get_and_renew", AsyncMock(return_value=None)
        ):
            answer = await stt.run_stt_job(job, user_id=7, status_message=None)
        self.assertEqual(answer.text, "hello")
        self.assertEqual(call.await_args.kwargs["key"], "synthetic-vertex-key-000000")
        self.assertEqual(call.await_args.kwargs["model"], "gemini/gemini-3.5-flash")

    async def test_vertex_title_generation_stays_on_vertex(self):
        job = stt.SttJob(
            models=[],
            api_key="synthetic-vertex-key-000000",
            attachments=[],
            provider="vertex",
        )
        generator = stt._job_title_generator(job, 7)
        raw = json.dumps(
            {
                "title": "Test",
                "title_as_file_name": "test",
                "short_description": "Test transcript",
            }
        )
        with patch.object(
            stt_providers, "transcribe_vertex", AsyncMock(return_value=raw)
        ) as call:
            result = await generator("transcript")
        self.assertEqual(result.title, "Test")
        self.assertEqual(call.await_args.kwargs["key"], job.api_key)

    async def test_vertex_auto_falls_through_without_loading_ai_studio_models(self):
        models = stt_providers.PROVIDERS["vertex"].auto_models
        with patch.object(
            stt_providers,
            "transcribe_vertex",
            AsyncMock(
                side_effect=[
                    stt_providers.ProviderError("vertex", 404),
                    '{"transcription":"hello"}',
                ]
            ),
        ) as transcribe, patch.object(
            stt.redis_util, "get_and_renew", AsyncMock(return_value=None)
        ), patch.object(
            stt.redis_util, "set_with_expiry", AsyncMock()
        ), patch.object(
            stt_models, "load_media_model"
        ) as load:
            answer = await stt._transcribe_with_retry(
                models=models,
                attachments=[],
                api_key=self.keys["vertex"],
                status_message=None,
                italics_marker="__",
                provider="vertex",
            )
        self.assertEqual(answer.model_name, models[1])
        self.assertEqual(
            [call.kwargs["model"] for call in transcribe.await_args_list],
            list(models[:2]),
        )
        self.assertTrue(
            all(
                call.kwargs["key"] == self.keys["vertex"]
                for call in transcribe.await_args_list
            )
        )
        load.assert_not_called()

    async def test_httpx_timeout_with_no_message_is_retried(self):
        self.assertTrue(stt._is_retriable_stt_error(httpx.ReadTimeout("")))
        self.assertFalse(
            stt._is_retriable_stt_error(stt_providers.ProviderError("vertex", 403))
        )

    async def test_runtime_vertex_key_error_shows_safe_actionable_detail(self):
        stt.set_provider_choice(7, "vertex")
        event = self.event()
        error = stt_providers.ProviderError("vertex", 403)
        job = stt.SttJob(
            models=[], api_key=self.keys["vertex"], attachments=[], provider="vertex"
        )
        with patch.object(stt, "prepare_stt_job", return_value=job), patch.object(
            stt, "run_stt_job", AsyncMock(side_effect=error)
        ), patch.object(stt, "_show_stt_status", AsyncMock()) as status, patch.object(
            stt.llm_util, "handle_llm_error", AsyncMock()
        ) as generic:
            await stt.llm_stt(cwd="/tmp/synthetic", event=event, log=False)
        status.assert_awaited_once_with(event.reply.return_value, str(error))
        self.assertIn("Check it with /provider", status.await_args.args[1])
        generic.assert_not_awaited()

    async def test_model_refusal_cache_is_isolated_by_provider(self):
        with patch.object(stt.redis_util, "set_with_expiry", AsyncMock()) as cache:
            await stt._mark_model_unavailable("synthetic-key", "model", "gemini")
            await stt._mark_model_unavailable("synthetic-key", "model", "vertex")
        first, second = cache.await_args_list
        self.assertNotEqual(first.args[0], second.args[0])
        self.assertNotIn("synthetic-key", str(cache.await_args_list))


class TransportTests(unittest.IsolatedAsyncioTestCase):
    async def request(self, action, payload, *, status=200):
        calls = []

        def respond(request):
            calls.append(request)
            return httpx.Response(status, json=payload)

        client = httpx.AsyncClient
        with patch.object(
            stt_providers.httpx,
            "AsyncClient",
            side_effect=lambda **kw: client(
                transport=httpx.MockTransport(respond), **kw
            ),
        ):
            result = await action()
        return result, calls

    async def test_key_validation_uses_selected_endpoint_and_header(self):
        for provider, host in (
            ("gemini", "generativelanguage.googleapis.com"),
            ("vertex", "aiplatform.googleapis.com"),
        ):
            with self.subTest(provider=provider):
                result, calls = await self.request(
                    lambda: stt_providers.validate_key(provider, "synthetic-key"),
                    {"totalTokens": 1},
                )
                request = calls[0]
                self.assertEqual(request.url.host, host)
                self.assertTrue(request.url.path.endswith(":countTokens"))
                self.assertEqual(request.headers["x-goog-api-key"], "synthetic-key")
                self.assertNotIn("synthetic-key", str(request.url))

    async def test_vertex_receives_inline_media_and_json_schema(self):
        attachment = SimpleNamespace(
            resolve_type=lambda: "audio/ogg", content_bytes=lambda: b"synthetic audio"
        )
        payload = {
            "candidates": [
                {
                    "content": {
                        "parts": [
                            {"text": "ignored thought", "thought": True},
                            {"text": '{"transcription":"hello"}'},
                        ]
                    }
                }
            ]
        }
        text, calls = await self.request(
            lambda: stt_providers.transcribe_vertex(
                model="gemini/gemini-3.5-flash",
                attachments=[attachment],
                key="synthetic-key",
                prompt="transcribe",
                schema=stt.TranscriptionResult,
            ),
            payload,
        )
        self.assertEqual(text, '{"transcription":"hello"}')
        request = calls[0]
        self.assertEqual(
            request.url.path,
            "/v1/publishers/google/models/gemini-3.5-flash:generateContent",
        )
        body = json.loads(request.content)
        self.assertEqual(
            base64.b64decode(body["contents"][0]["parts"][1]["inlineData"]["data"]),
            b"synthetic audio",
        )
        self.assertEqual(
            body["generationConfig"]["responseMimeType"], "application/json"
        )
        self.assertIn(
            "transcription",
            body["generationConfig"]["responseJsonSchema"]["properties"],
        )

    async def test_provider_error_does_not_echo_raw_error_body(self):
        with self.assertRaises(stt_providers.ProviderError) as raised:
            await self.request(
                lambda: stt_providers.validate_key("vertex", "synthetic-key"),
                {"error": {"message": "sensitive raw upstream detail"}},
                status=403,
            )
        self.assertNotIn("sensitive raw upstream detail", str(raised.exception))
        self.assertIn("403", str(raised.exception))

    async def test_malformed_success_payload_cannot_validate_a_key(self):
        for payload in ({}, {"totalTokens": "1"}, []):
            with self.subTest(payload=payload), self.assertRaises(RuntimeError):
                await self.request(
                    lambda: stt_providers.validate_key("vertex", "synthetic-key"),
                    payload,
                )

    async def test_client_is_created_inside_the_proxy_context(self):
        token = stt.llm_util.set_llm_gemini_proxy("http://synthetic-proxy.invalid:8080")
        try:
            # The installed HTTPX patch applies the task-local setting at construction.
            async with httpx.AsyncClient() as client:
                proxies = [p for p in client._mounts.values() if p is not None]
                self.assertTrue(proxies)
                self.assertTrue(
                    any(
                        "synthetic-proxy.invalid" in str(p._pool._proxy_url)
                        for p in proxies
                    )
                )
        finally:
            stt.llm_util.reset_llm_gemini_proxy(token)

    async def test_vertex_menu_omits_developer_only_aliases_and_speech_model(self):
        options = stt_providers.model_options("vertex")
        self.assertIn("auto", options)
        self.assertIn("3.5-flash", options)
        self.assertNotIn("flash-latest", options)
        self.assertNotIn("3.5-transcribe", options)


if __name__ == "__main__":
    unittest.main()
