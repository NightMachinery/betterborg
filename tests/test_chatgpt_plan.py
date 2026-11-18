from concurrent.futures import ThreadPoolExecutor
import json
import time
from urllib.parse import urlencode

import pytest

from uniborg import chatgpt_plan as plan


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
