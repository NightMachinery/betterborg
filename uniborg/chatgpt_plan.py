"""Pilot credentials for the Sign in with ChatGPT Responses API."""

import argparse
import base64
from contextlib import contextmanager
import fcntl
import hashlib
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
from pathlib import Path
import secrets
import sys
import tempfile
import time
from urllib.parse import parse_qs, urlencode, urlsplit
import uuid

import httpx


ISSUER = "https://auth.openai.com"
DISCOVERY = f"{ISSUER}/.well-known/openid-configuration"
AUTHORIZE = f"{ISSUER}/api/accounts/authorize"
TOKEN_ENDPOINT = f"{ISSUER}/api/accounts/oauth/token"
RESOURCE = "https://api.openai.com/v1"
PLAN_SCOPE = "chatgpt.tokens.use.direct"
SCOPES = f"openid profile email offline_access resource.invoke {PLAN_SCOPE}"
DYNAMIC_CLIENT = "dynamic_agent_client"
CALLBACK_PATH = "/auth/callback"
CONNECT_NEEDED = "Your ChatGPT plan connection needs setup or renewal. Contact the bot operator."
USAGE_URL = "https://chatgpt.com/settings/usage"
CONTEXT_BYTES = 48 * 1024


class PlanError(Exception):
    """An error safe to relay without revealing a credential or OAuth payload."""


def request_error(code=None, *, status=None):
    if code == "subscription_sharing_usage_limit_exceeded":
        return f"Your ChatGPT plan has reached a usage limit. Manage usage at {USAGE_URL}."
    if code == "subscription_sharing_usage_unavailable":
        return "ChatGPT plan usage is temporarily unavailable. Try again later."
    if status in (401, 403):
        return CONNECT_NEEDED
    if code == "model_not_found":
        return "This model is unavailable through your ChatGPT plan. Try another GPT model."
    return "The ChatGPT plan preview could not complete the request."


def _root():
    return Path.home() / ".borg" / "chatgpt-plan"


def _path(user_id):
    if type(user_id) is not int or user_id <= 0:
        raise ValueError("Expected a positive Telegram user ID.")
    return _root() / f"{user_id}.json"


def linked(user_id):
    return type(user_id) is int and user_id > 0 and _path(user_id).is_file()


@contextmanager
def _locked(path):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.parent.stat().st_mode & 0o077:
        raise PlanError("The ChatGPT plan credential directory must have mode 0700.")
    fd = os.open(path.with_suffix(".lock"), os.O_CREAT | os.O_RDWR, 0o600)
    with os.fdopen(fd, "r+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def _read(path):
    try:
        with path.open() as stream:
            if os.fstat(stream.fileno()).st_mode & 0o077:
                raise PlanError("The ChatGPT plan credential file must have mode 0600.")
            return json.load(stream)
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        raise PlanError("The ChatGPT plan credential record could not be read.") from None


def _write(path, record):
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as stream:
        temporary = Path(stream.name)
        try:
            json.dump(record, stream)
            stream.flush()
            os.fsync(stream.fileno())
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)


def _host_id():
    path = _root() / "host.json"
    with _locked(path):
        host_id = _read(path)
        if host_id is None:
            host_id = uuid.uuid4().urn
            _write(path, host_id)
        return host_id


def _token_request(form):
    try:
        response = httpx.post(TOKEN_ENDPOINT, data={**form, "resource": RESOURCE}, timeout=15)
        response.raise_for_status()
        payload = response.json()
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code in (400, 401, 403):
            raise PlanError(CONNECT_NEEDED) from None
        raise PlanError("ChatGPT sign-in is temporarily unavailable.") from None
    except (httpx.HTTPError, ValueError):
        raise PlanError("ChatGPT sign-in is temporarily unavailable.") from None
    if not isinstance(payload, dict):
        raise PlanError("ChatGPT returned an invalid token response.")
    return payload


def _token_record(payload, previous=None):
    previous = previous or {}
    scope = payload.get("scope", previous.get("scope", ""))
    if not isinstance(scope, str) or PLAN_SCOPE not in scope.split():
        raise PlanError("ChatGPT did not grant permission to use the plan allowance.")
    access = payload.get("access_token")
    refresh = payload.get("refresh_token")
    expiry = payload.get("expires_in")
    if (
        not isinstance(access, str) or not access
        or not isinstance(refresh, str) or not refresh
        or type(expiry) not in (int, float) or expiry <= 0
    ):
        raise PlanError("ChatGPT returned incomplete credentials.")
    return {
        **previous,
        "access_token": access,
        "refresh_token": refresh,
        "expires_at": time.time() + expiry,
        "scope": scope,
    }


def access_token(user_id):
    """Return USER_ID's bearer access token, refreshing under an interprocess lock."""
    path = _path(user_id)
    if not path.is_file():
        raise PlanError(CONNECT_NEEDED)
    with _locked(path):
        record = _read(path)
        if not record:
            raise PlanError(CONNECT_NEEDED)
        if float(record["expires_at"]) <= time.time() + 30:
            payload = _token_request({
                "grant_type": "refresh_token",
                "client_id": record["client_id"],
                "refresh_token": record["refresh_token"],
            })
            record = _token_record(payload, record)
            _write(path, record)
        return record["access_token"]


def _discovery():
    response = httpx.get(DISCOVERY, timeout=15)
    response.raise_for_status()
    return response.json()


def _auth_endpoint(metadata, name):
    url = metadata[name]
    parsed = urlsplit(url)
    if parsed.scheme != "https" or parsed.netloc != "auth.openai.com":
        raise PlanError("OpenAI published an unexpected authentication endpoint.")
    return url


def _verify_id_token(token, client_id, nonce):
    import jwt

    response = httpx.get(_auth_endpoint(_discovery(), "jwks_uri"), timeout=15)
    response.raise_for_status()
    keys = response.json()["keys"]
    try:
        kid = jwt.get_unverified_header(token)["kid"]
        key = next(key for key in keys if key.get("kid") == kid)
        claims = jwt.decode(
            token, jwt.PyJWK.from_dict(key).key, algorithms=["RS256"],
            audience=client_id, issuer=ISSUER,
            options={"require": ["sub", "iss", "aud", "exp", "nonce"]},
        )
    except (jwt.PyJWTError, KeyError, StopIteration, TypeError):
        raise PlanError("ChatGPT account verification failed.") from None
    if claims["nonce"] != nonce:
        raise PlanError("The ChatGPT authorization did not match this login.")
    return claims["sub"]


def _callback(port, state, parameters):
    received = None

    class Callback(BaseHTTPRequestHandler):
        def setup(self):
            super().setup()
            self.connection.settimeout(5)

        def log_message(self, *args):
            pass

        def do_GET(self):
            nonlocal received
            parsed = urlsplit(self.path)
            query = parse_qs(parsed.query)
            if parsed.path != CALLBACK_PATH or query.get("state") != [state]:
                self.send_error(404)
                return
            received = query
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(b"Betterborg received the response. Return to your terminal.")

    with HTTPServer(("127.0.0.1", port), Callback) as server:
        redirect = f"http://127.0.0.1:{server.server_port}{CALLBACK_PATH}"
        print(f"Open this URL in your browser:\n{AUTHORIZE}?{urlencode({**parameters, 'redirect_uri': redirect})}", flush=True)
        deadline = time.monotonic() + 300
        while received is None and (remaining := deadline - time.monotonic()) > 0:
            server.timeout = remaining
            server.handle_request()
    if received is None:
        raise PlanError("ChatGPT sign-in timed out.")
    if "error" in received:
        raise PlanError("ChatGPT authorization was declined or interrupted.")
    return redirect, received


def connect(user_id, port):
    host_id = _host_id()
    path = _path(user_id)
    with _locked(path):
        previous = _read(path) or {}
    state, nonce, verifier = (secrets.token_urlsafe(32) for _ in range(3))
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    client_id = previous.get("client_id", DYNAMIC_CLIENT)
    params = {
        "client_id": client_id, "ext_agent_host_id": host_id,
        "response_type": "code", "scope": SCOPES, "resource": RESOURCE,
        "state": state, "nonce": nonce, "code_challenge_method": "S256",
        "code_challenge": challenge,
    }
    if client_id == DYNAMIC_CLIENT:
        params["agent_name_hint"] = "Betterborg"
    redirect, query = _callback(port, state, params)
    codes, issued = query.get("code", []), query.get("client_id", [])
    if len(codes) != 1 or len(issued) > 1:
        raise PlanError("ChatGPT returned an invalid authorization callback.")
    if client_id == DYNAMIC_CLIENT:
        if not issued or not issued[0].startswith("oaiapp_"):
            raise PlanError("ChatGPT did not issue an OAuth client ID.")
        client_id = issued[0]
    elif issued and issued[0] != client_id:
        raise PlanError("ChatGPT returned a different OAuth client ID.")
    with _locked(path):
        current = _read(path) or {}
        if current != previous:
            raise PlanError("Another operation changed this connection. Start again.")
        payload = _token_request({
            "grant_type": "authorization_code", "client_id": client_id,
            "code": codes[0], "code_verifier": verifier, "redirect_uri": redirect,
        })
        subject = _verify_id_token(payload.get("id_token"), client_id, nonce)
        if previous.get("subject", subject) != subject:
            raise PlanError("This is a different ChatGPT account. Disconnect the previous one first.")
        record = _token_record(payload, {"subject": subject, "client_id": client_id})
        _write(path, record)


def disconnect(user_id):
    path = _path(user_id)
    with _locked(path):
        record = _read(path)
        if record:
            # Stop local use even if the remote token has already expired.
            path.unlink()
    if not record:
        return True
    try:
        response = httpx.post(
            _auth_endpoint(_discovery(), "revocation_endpoint"), timeout=15,
            data={"token": record["refresh_token"], "token_type_hint": "refresh_token",
                  "client_id": record["client_id"]},
        )
        return response.status_code == 200
    except (httpx.HTTPError, ValueError, KeyError, PlanError):
        return False


def main():
    parser = argparse.ArgumentParser(description="Link a Telegram user to the ChatGPT plan pilot.")
    parser.add_argument("action", choices=("connect", "disconnect"))
    parser.add_argument("user_id", type=int, help="Numeric Telegram user ID")
    parser.add_argument("--port", type=int, default=1455, help="OAuth loopback port")
    args = parser.parse_args()
    try:
        if args.action == "connect":
            connect(args.user_id, args.port)
            print("Connected. Select /openaiProvider chatgpt-plan in Betterborg.")
        elif disconnect(args.user_id):
            print("Disconnected.")
        else:
            print("Disconnected locally. Revoke Betterborg in ChatGPT Settings to end the remote session.")
    except PlanError as exc:
        parser.exit(1, f"{exc}\n")
    except (httpx.HTTPError, OSError, KeyError, TypeError, ValueError):
        parser.exit(1, "ChatGPT plan setup failed. Check the loopback port and network connection.\n")


if __name__ == "__main__":
    main()
