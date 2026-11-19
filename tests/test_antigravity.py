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
