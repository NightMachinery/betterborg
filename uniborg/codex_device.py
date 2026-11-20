"""Codex device authorization over HTTPS, with one connection per Telegram user."""

import asyncio
from dataclasses import dataclass
import re
import time
import uuid

import httpx

from uniborg import chatgpt_plan


ISSUER = chatgpt_plan.ISSUER
CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"  # Public identifier from the Codex client.
DEVICE_URL = f"{ISSUER}/codex/device"
DEVICE_API = f"{ISSUER}/api/accounts/deviceauth"
TOKEN_URL = f"{ISSUER}/oauth/token"
BACKEND_URL = "https://chatgpt.com/backend-api/codex"
AUTH_CLAIM = "https://api.openai.com/auth"
LOGIN_TIMEOUT = 15 * 60
CONNECT_NEEDED = "Reconnect your ChatGPT account with /Codex_Login in my private chat."
USER_CODE = re.compile(r"[A-Z0-9-]{4,24}")


@dataclass(frozen=True, repr=False)
class DeviceLogin:
    user_id: int
    user_code: str
    device_auth_id: str
    interval: int
    previous: dict
    deadline: float


def _value(data, name, limit=16384):
    value = data.get(name)
    if not isinstance(value, str) or not value or len(value) > limit:
        raise chatgpt_plan.PlanError("OpenAI returned an invalid Codex login response.")
    return value


def _response(response):
    try:
        response.raise_for_status()
        value = response.json()
    except (httpx.HTTPError, ValueError):
        raise chatgpt_plan.PlanError("OpenAI could not complete the Codex sign-in request.") from None
    if not isinstance(value, dict):
        raise chatgpt_plan.PlanError("OpenAI returned an invalid Codex login response.")
    return value


async def start(user_id):
    path = chatgpt_plan._path(user_id)
    previous = await asyncio.to_thread(chatgpt_plan._read, path)
    async with httpx.AsyncClient(timeout=15) as client:
        try:
            response = await client.post(f"{DEVICE_API}/usercode", json={"client_id": CLIENT_ID})
        except httpx.HTTPError:
            raise chatgpt_plan.PlanError("OpenAI sign-in is temporarily unavailable.") from None
    data = _response(response)
    code = _value(data, "user_code" if "user_code" in data else "usercode", limit=24)
    try:
        interval = int(data.get("interval", 5))
    except (TypeError, ValueError):
        interval = 5
    if not USER_CODE.fullmatch(code) or not 0 <= interval <= 60:
        raise chatgpt_plan.PlanError("OpenAI returned an invalid Codex sign-in code.")
    return DeviceLogin(
        user_id, code, _value(data, "device_auth_id", limit=1024), max(2, interval),
        chatgpt_plan._connection_identity(previous), time.monotonic() + LOGIN_TIMEOUT,
    )


async def complete(login):
    remaining = max(0, login.deadline - time.monotonic())
    try:
        async with asyncio.timeout(remaining), httpx.AsyncClient(timeout=15) as client:
            while True:
                response = await client.post(f"{DEVICE_API}/token", json={
                    "device_auth_id": login.device_auth_id, "user_code": login.user_code,
                })
                if response.status_code in (403, 404):
                    await asyncio.sleep(login.interval)
                    continue
                grant = _response(response)
                break
            payload = _response(await client.post(TOKEN_URL, data={
                "grant_type": "authorization_code", "client_id": CLIENT_ID,
                "redirect_uri": f"{ISSUER}/deviceauth/callback",
                "code": _value(grant, "authorization_code"),
                "code_verifier": _value(grant, "code_verifier"),
            }))
    except TimeoutError:
        raise chatgpt_plan.PlanError("The Codex sign-in code expired. Send /Codex_Login to try again.") from None
    except httpx.HTTPError:
        raise chatgpt_plan.PlanError("OpenAI sign-in is temporarily unavailable.") from None
    save = asyncio.create_task(asyncio.to_thread(_save, login, payload))
    try:
        await asyncio.shield(save)
    except asyncio.CancelledError:
        # Wait for the interprocess write before a caller unlinks the record.
        await asyncio.gather(save, return_exceptions=True)
        raise


def _verified_account(payload):
    try:
        claims = chatgpt_plan._verified_id_claims(_value(payload, "id_token"), CLIENT_ID)
        subject = _value(claims, "sub", limit=256)
        provider_claims = claims[AUTH_CLAIM]
        account = _value(provider_claims, "chatgpt_account_id", limit=128)
        if not re.fullmatch(r"[a-zA-Z0-9_-]+", account):
            raise ValueError
    except (httpx.HTTPError, KeyError, AttributeError, ValueError):
        raise chatgpt_plan.PlanError("OpenAI account verification failed.") from None
    return subject, account


def _record(payload, previous):
    access = _value(payload, "access_token")
    refresh = _value({**previous, **payload}, "refresh_token")
    expires = payload.get("expires_in")
    if type(expires) not in (int, float) or not 0 < expires <= 30 * 86400:
        # Some native responses encode the expiry only in the access JWT.
        import jwt
        try:
            expires = jwt.decode(access, options={"verify_signature": False})["exp"] - time.time()
        except (jwt.PyJWTError, KeyError, TypeError, ValueError):
            raise chatgpt_plan.PlanError("OpenAI returned incomplete Codex credentials.") from None
        if not 0 < expires <= 30 * 86400:
            raise chatgpt_plan.PlanError("OpenAI returned expired Codex credentials.")
    return {**previous, "access_token": access, "refresh_token": refresh,
            "expires_at": time.time() + expires}


def _save(login, payload):
    subject, account = _verified_account(payload)
    path = chatgpt_plan._path(login.user_id)
    with chatgpt_plan._locked(path):
        if chatgpt_plan._connection_identity(chatgpt_plan._read(path)) != login.previous:
            raise chatgpt_plan.PlanError("Another operation changed this connection. Start again.")
        if login.previous.get("subject", subject) != subject:
            raise chatgpt_plan.PlanError("Disconnect the previous ChatGPT account before linking a different one.")
        chatgpt_plan._write(path, _record(payload, {
            "mode": "codex-device", "subject": subject, "account_id": account,
            "client_id": CLIENT_ID, "link_id": uuid.uuid4().hex,
        }))


def refresh(record):
    try:
        response = httpx.post(TOKEN_URL, data={
            "grant_type": "refresh_token", "client_id": CLIENT_ID,
            "refresh_token": record["refresh_token"],
        }, timeout=15)
    except httpx.HTTPError:
        raise chatgpt_plan.PlanError("OpenAI sign-in is temporarily unavailable.") from None
    if response.status_code in (400, 401, 403):
        raise chatgpt_plan.PlanError(CONNECT_NEEDED)
    payload = _response(response)
    if payload.get("id_token") and _verified_account(payload) != (
        record["subject"], record["account_id"]
    ):
        raise chatgpt_plan.PlanError(CONNECT_NEEDED)
    return _record(payload, record)
