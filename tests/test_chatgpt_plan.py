from concurrent.futures import ThreadPoolExecutor
import asyncio
import json
import time
from urllib.parse import urlencode

import pytest
import httpx

from uniborg import chatgpt_plan as plan
from uniborg import codex_device


def token_payload(access, refresh):
    return {
        "access_token": access,
        "refresh_token": refresh,
        "scope": plan.SCOPES,
        "expires_in": 3600,
    }


@pytest.fixture
def credentials_dir(monkeypatch, tmp_path):
    root = tmp_path / "profiles"
    monkeypatch.setattr(plan, "_root", lambda: root)
    return root


def test_link_exchanges_the_callback_with_pkce_and_reuses_registration(
    credentials_dir, monkeypatch
):
    authorization = []
    exchanges = []

    def callback(port, state, params):
        assert state == params["state"]
        authorization.append(params)
        return f"http://127.0.0.1:{port}/auth/callback", {
            "code": ["one-time-code"], "client_id": ["oaiapp_pilot"], "state": [state]
        }

    def token_request(form):
        exchanges.append(form)
        return token_payload("access", "refresh") | {"id_token": "test-id-token"}

    monkeypatch.setattr(plan, "_callback", callback)
    monkeypatch.setattr(plan, "_token_request", token_request)
    monkeypatch.setattr(plan, "_verify_id_token", lambda *args: "test-subject")
    plan.connect(111, 1455)
    plan.connect(111, 1456)

    assert authorization[0]["client_id"] == plan.DYNAMIC_CLIENT
    assert authorization[1]["client_id"] == "oaiapp_pilot"
    assert authorization[0]["ext_agent_host_id"] == authorization[1]["ext_agent_host_id"]
    assert authorization[0]["code_challenge"] != exchanges[0]["code_verifier"]
    assert exchanges[0]["redirect_uri"] == "http://127.0.0.1:1455/auth/callback"
    assert exchanges[0]["client_id"] == "oaiapp_pilot"
    assert (credentials_dir / "111.json").stat().st_mode & 0o777 == 0o600
    assert plan.access_token(111) == "access"
    with pytest.raises(plan.PlanError, match="needs setup"):
        plan.access_token(222)


def test_rotating_refresh_is_serialized_and_isolated(credentials_dir, monkeypatch):
    for user in (111, 222):
        path = plan._path(user)
        with plan._locked(path):
            record = plan._token_record(
                token_payload(f"access-{user}", f"refresh-{user}"),
                {"client_id": f"oaiapp_{user}"},
            )
            if user == 111:
                record["expires_at"] = time.time() - 1
            plan._write(path, record)

    exchanges = []

    def token_request(form):
        exchanges.append(form)
        return token_payload("new-access", "new-refresh")

    monkeypatch.setattr(plan, "_token_request", token_request)
    with ThreadPoolExecutor(max_workers=2) as executor:
        assert list(executor.map(plan.access_token, (111, 111))) == ["new-access"] * 2
    assert len(exchanges) == 1
    assert exchanges[0]["refresh_token"] == "refresh-111"
    assert json.loads((credentials_dir / "111.json").read_text())["refresh_token"] == "new-refresh"
    assert plan.access_token(222) == "access-222"


def test_copied_browser_return_link_matches_exact_port_and_oauth_state(credentials_dir):
    login = plan.start_login(111)
    params = {"state": login.parameters["state"], "code": "one-time"}
    valid = f"{login.telegram_redirect}?{urlencode(params)}"
    assert plan.is_callback_url(valid)
    assert plan.telegram_callback(login, valid)["code"] == ["one-time"]
    assert plan.telegram_callback(login, valid.replace(login.parameters["state"], "wrong")) is None
    assert plan.telegram_callback(login, valid.replace("127.0.0.1", "localhost")) is None


def test_device_approval_binds_tokens_to_the_initiating_telegram_user(
    credentials_dir, monkeypatch
):
    requests = []

    def respond(request):
        requests.append(request)
        if request.url.path.endswith("/usercode"):
            assert json.loads(request.content) == {"client_id": codex_device.CLIENT_ID}
            return httpx.Response(200, json={
                "user_code": "TEST-1234", "device_auth_id": "test-device", "interval": "5",
            })
        if request.url.path.endswith("/deviceauth/token"):
            assert json.loads(request.content) == {
                "user_code": "TEST-1234", "device_auth_id": "test-device",
            }
            return httpx.Response(200, json={
                "authorization_code": "test-grant", "code_verifier": "test-verifier",
            })
        assert request.url == codex_device.TOKEN_URL
        assert "grant_type=authorization_code" in request.content.decode()
        assert "code_verifier=test-verifier" in request.content.decode()
        return httpx.Response(200, json=token_payload("device-access", "device-refresh"))

    factory = httpx.AsyncClient
    monkeypatch.setattr(codex_device.httpx, "AsyncClient", lambda **kwargs: factory(
        transport=httpx.MockTransport(respond), **kwargs,
    ))
    monkeypatch.setattr(codex_device, "_verified_account", lambda payload: ("subject", "account"))

    async def authorize():
        await codex_device.complete(await codex_device.start(111))

    asyncio.run(authorize())
    assert len(requests) == 3
    assert plan.device_linked(111)
    assert plan.credentials(111) == ("device-access", "account")
    assert (credentials_dir / "111.json").stat().st_mode & 0o777 == 0o600
    assert not plan.linked(222)


def test_device_sign_in_cannot_overwrite_a_replaced_connection(credentials_dir, monkeypatch):
    login = codex_device.DeviceLogin(111, "TEST", "id", 5, {}, 0)
    path = plan._path(111)
    with plan._locked(path):
        plan._write(path, plan._token_record(token_payload("first", "refresh"), {
            "client_id": "oaiapp_first", "subject": "first", "link_id": "new",
        }))
    monkeypatch.setattr(codex_device, "_verified_account", lambda payload: ("second", "account"))
    with pytest.raises(plan.PlanError, match="changed this connection"):
        codex_device._save(login, token_payload("second", "device-refresh"))
    assert plan.credentials(111) == ("first", None)
