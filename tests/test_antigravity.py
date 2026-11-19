"""Owner-bound Google login and the Telegram ACP tool boundary."""

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from urllib.parse import urlencode

import pytest
from test_llm_chat_vertex import plugin

from uniborg import antigravity, gemini_providers


def login_link():
    params = urlencode(
        {
            "redirect_uri": "http://127.0.0.1:45678/",
            "state": "test-state",
        }
    )
    return antigravity.LoginLink.from_output(
        f"Open the following link: https://accounts.google.com/o/oauth2/v2/auth?{params}"
    )


def callback_query():
    return "http://127.0.0.1:45678/?state=test-state&code=test-code"


def test_accepts_only_matching_google_issued_return():
    link = login_link()
    assert link.callback(callback_query()) == "/?state=test-state&code=test-code"
    for bad in (
        callback_query().replace("45678", "45679"),
        callback_query().replace("test-state", "other"),
        callback_query().replace("127.0.0.1", "10.0.0.1"),
        callback_query() + "&state=test-state",
        callback_query() + "#fragment",
    ):
        assert link.callback(bad) is None
    assert "test-state" not in repr(link)


def test_callback_is_owner_bound_and_single_use():
    async def scenario():
        backend = antigravity.Antigravity()
        release = asyncio.Event()
        handled = []
        client = SimpleNamespace(
            login_url=asyncio.get_running_loop().create_future(),
            complete_login=AsyncMock(),
        )
        client.login_url.set_result(login_link())

        async def authenticate(method, **params):
            assert (method, params["methodId"]) == ("authenticate", "oauth-personal")
            await release.wait()

        async def complete(link, target):
            handled.append(target)
            release.set()

        client.request = authenticate
        client.complete_login.side_effect = complete

        @asynccontextmanager
        async def worker(user_id):
            assert user_id == 1
            yield client

        with (
            patch.object(backend, "worker", worker),
            patch.object(antigravity, "linked", side_effect=[False, True]),
        ):
            await backend.start_login(1)
            assert not await backend.finish_login(2, callback_query())
            assert not await backend.finish_login(
                1, callback_query().replace("test-state", "wrong")
            )
            first, replay = await asyncio.gather(
                backend.finish_login(1, callback_query()),
                backend.finish_login(1, callback_query()),
            )
            assert (first, replay) == (True, False)
            assert handled == ["/?state=test-state&code=test-code"]

    asyncio.run(scenario())


def test_session_exposes_only_requested_web_search_and_no_client_tools():
    async def scenario():
        client = antigravity.Client(None, "worker")
        client.request = AsyncMock(return_value={"sessionId": "unique"})
        await client.new_session(search=True)
        client.request.assert_awaited_once_with(
            "session/new",
            cwd="/work",
            mcpServers=[],
            _meta={"antigravity": {"enabledTools": ["search_web"]}},
        )
        client.send = AsyncMock()
        await client.dispatch({"id": 12, "method": "session/request_permission"})
        client.send.assert_awaited_once_with(
            id=12,
            result={"outcome": {"outcome": "cancelled"}},
        )
        await client.dispatch({"id": 13, "method": "terminal/create"})
        assert client.send.call_args.kwargs["error"]["code"] == -32601
        search = {
            "sessionId": "unique",
            "toolCall": {"title": "Run search_web?", "kind": "search"},
            "options": [{"optionId": "allow", "kind": "allow_once"}],
        }
        assert client.permission(search) == {"outcome": "selected", "optionId": "allow"}
        assert client.permission({**search, "sessionId": "other"}) == {
            "outcome": "cancelled"
        }
        search["toolCall"]["kind"] = "execute"
        assert client.permission(search) == {"outcome": "cancelled"}

    asyncio.run(scenario())


def test_an_admin_must_use_their_own_private_chat():
    with patch.object(plugin.util, "is_admin_by_id", side_effect=lambda uid: uid == 7):
        for sender, chat, private, outgoing, allowed in [
            (7, 7, True, False, True),
            (7, 8, True, False, False),
            (8, 8, True, False, False),
            (7, 7, False, False, False),
            (7, 7, True, True, False),
        ]:
            assert (
                plugin._antigravity_admin(
                    SimpleNamespace(
                        sender_id=sender,
                        chat_id=chat,
                        is_private=private,
                        out=outgoing,
                    )
                )
                is allowed
            )


def test_antigravity_dispatch_has_no_key_fallback():
    async def scenario():
        with (
            patch.object(antigravity, "linked", return_value=False),
            patch.object(
                plugin,
                "_request_credentials",
                new_callable=AsyncMock,
            ) as credentials,
        ):
            request = plugin.RequestModel(
                model=plugin.DEFAULT_MODEL, service=gemini_providers.AI_STUDIO
            )
            keyed = await plugin._keyed_request(
                request,
                user_id=7,
                codex_access=True,
                service=gemini_providers.ANTIGRAVITY,
            )
            assert keyed.api_key is None
            credentials.assert_not_awaited()

    asyncio.run(scenario())


def test_select_only_the_requested_model_on_the_accounts_list():
    session = {
        "models": {
            "availableModels": [
                {"modelId": "gemini-3.8-flash-high"},
                {"modelId": "gemini-4-hydrogen-xhigh"},
            ]
        }
    }
    assert (
        antigravity.select_model(session, plugin.DEFAULT_MODEL)
        == "gemini-3.8-flash-high"
    )
    assert (
        antigravity.select_model(
            session,
            "gemini/gemini-4-hydrogen",
            "xhigh",
        )
        == "gemini-4-hydrogen-xhigh"
    )
    with pytest.raises(antigravity.AntigravityError, match="unavailable"):
        antigravity.select_model(session, "gemini/gemini-4-neon")


def test_worker_slot_is_shared_across_clients(tmp_path, monkeypatch):
    monkeypatch.setattr(antigravity, "PROFILES", tmp_path)
    with (
        antigravity._worker_slot(),
        pytest.raises(antigravity.AntigravityError, match="already in use"),
        antigravity._worker_slot(),
    ):
        pytest.fail("Two workers held the same server slot.")
    with antigravity._worker_slot():
        pass


def test_quota_uses_rich_markdown_and_skips_empty_session():
    async def scenario():
        event = SimpleNamespace(is_private=True)
        with (
            patch.object(
                plugin.tg_raw,
                "send_rich_markdown",
                new_callable=AsyncMock,
            ) as send,
            patch.object(plugin, "_thread_topic_id", return_value=None),
        ):
            await plugin._show_antigravity_quota(
                event,
                "### Session Usage\n- data not available yet\n\n### Quota Limits\n- **Weekly**",
            )
        assert send.await_args.args[1].endswith("### Quota Limits\n- **Weekly**")
        assert "Session Usage" not in send.await_args.args[1]

    asyncio.run(scenario())


def test_signin_callback_updates_the_original_card():
    async def scenario():
        event = SimpleNamespace(
            sender_id=7, raw_text=callback_query(), delete=AsyncMock()
        )
        card = SimpleNamespace(edit=AsyncMock())
        task = asyncio.get_running_loop().create_future()
        task.set_result(True)
        with (
            patch.object(plugin, "_antigravity_admin", return_value=True),
            patch.object(plugin, "ANTIGRAVITY_SIGN_IN_CARDS", {7: card}),
            patch.object(antigravity.backend, "accept_callback", return_value=task),
            patch.object(plugin.user_manager, "set_gemini_provider") as set_provider,
            pytest.raises(plugin.events.StopPropagation),
        ):
            await plugin.antigravity_callback_handler(event)
        event.delete.assert_awaited_once()
        set_provider.assert_called_once_with(7, gemini_providers.ANTIGRAVITY)
        card.edit.assert_awaited_once()
        assert "now your Gemini provider" in card.edit.await_args.args[0]
        assert card.edit.await_args.kwargs["buttons"] == []

    asyncio.run(scenario())


def test_only_completed_inline_acp_image_is_delivered():
    async def scenario():
        import base64

        client = antigravity.Client(None, "worker")
        client.request = AsyncMock(return_value={"sessionId": "unique"})
        await client.new_session(images=True)
        assert client.request.await_args.kwargs["_meta"]["antigravity"][
            "enabledTools"
        ] == ["generate_image"]
        client.on_image = AsyncMock()
        data = b"\x89PNG\r\n\x1a\nfixture"
        frame = {
            "method": "session/update",
            "params": {
                "sessionId": "unique",
                "update": {
                    "sessionUpdate": "tool_call_update",
                    "status": "in_progress",
                    "content": [
                        {
                            "type": "content",
                            "content": {
                                "type": "image",
                                "mimeType": "image/png",
                                "data": base64.b64encode(data).decode(),
                            },
                        }
                    ],
                    "rawOutput": {"imagePath": "/ignored"},
                },
            },
        }
        await client.dispatch(frame)
        client.on_image.assert_not_awaited()
        frame["params"]["update"]["status"] = "completed"
        await client.dispatch(frame)
        client.on_image.assert_awaited_once_with(data, "png")

    asyncio.run(scenario())


@pytest.mark.parametrize("cancel", [False, True])
def test_transcription_waits_without_starting_a_second_worker(
    tmp_path,
    monkeypatch,
    cancel,
):
    monkeypatch.setattr(antigravity, "PROFILES", tmp_path)
    monkeypatch.setattr(antigravity, "WORKER_POLL_INTERVAL", 0.01)

    async def run():
        active = antigravity.Antigravity()
        transcribe = antigravity.Antigravity()
        waiting = asyncio.Event()
        started = []

        @asynccontextmanager
        async def fake_worker(owner):
            started.append(owner)
            yield owner

        monkeypatch.setattr(antigravity, "_worker", fake_worker)

        async def request():
            async with transcribe.worker(2, wait=True, on_wait=waiting.set) as worker:
                return worker

        async with active.worker(1):
            task = asyncio.create_task(request())
            try:
                await asyncio.wait_for(waiting.wait(), 1)
                assert started == [1]
                if cancel:
                    task.cancel()
                    with pytest.raises(asyncio.CancelledError):
                        await task
            finally:
                if not waiting.is_set():
                    task.cancel()
        if cancel:
            assert started == [1]
        else:
            assert await asyncio.wait_for(task, 1) == 2
            assert started == [1, 2]
        with antigravity._worker_slot():
            pass

    asyncio.run(run())


def test_wait_expiry_never_claims_the_busy_slot(tmp_path, monkeypatch):
    monkeypatch.setattr(antigravity, "PROFILES", tmp_path)
    monkeypatch.setattr(antigravity, "WORKER_WAIT_TIMEOUT", 0.02)

    async def run():
        backend = antigravity.Antigravity()
        with antigravity._worker_slot():
            with pytest.raises(antigravity.WorkerBusy):
                async with backend.worker(1):
                    pytest.fail("Account management should still fail fast.")
            with pytest.raises(antigravity.AntigravityError, match="stayed busy"):
                async with backend.worker(1, wait=True):
                    pytest.fail("A timed-out job should never start.")

    asyncio.run(run())
