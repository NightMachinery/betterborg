"""Use the shared Antigravity server's typed turn API over a Unix socket."""

from dataclasses import dataclass
import json
import os
import re
from pathlib import Path

import httpx

from uniborg import antigravity_protocol as protocol


class GatewayError(Exception):
    code: str

    def __init__(self, code, *, request_id=None, retry_after_seconds=None):
        self.code = code
        self.request_id = request_id
        self.retry_after_seconds = retry_after_seconds
        super().__init__(code)


def _turn_error(response, event):
    request_id = response.headers.get("X-Request-ID", "")
    retry_after = event.get("retry_after_seconds")
    return GatewayError(
        event["code"],
        request_id=request_id if re.fullmatch(r"[0-9a-f]{32}", request_id) else None,
        retry_after_seconds=retry_after if type(retry_after) is int and retry_after > 0 else None,
    )


@dataclass(frozen=True)
class OperatorEndpoint:
    socket: str
    token_file: Path
    account: str

    @classmethod
    def for_user(cls, user_id):
        selected = os.environ.get("borg_antigravity_gateway_owner")
        if selected is not None:
            config = {
                "owner": selected,
                "socket": os.environ.get("borg_antigravity_gateway_socket", ""),
                "token_file": os.environ.get("borg_antigravity_gateway_token_file", ""),
                "account": os.environ.get("borg_antigravity_gateway_account", "main"),
            }
        else:
            path = Path.home() / ".config" / "betterborg" / "antigravity-gateway.json"
            try:
                config = json.loads(path.read_text())
            except FileNotFoundError:
                return None
            except (OSError, ValueError):
                raise GatewayError("worker_unavailable") from None
        if not isinstance(config, dict):
            raise GatewayError("worker_unavailable")
        if type(user_id) is not int or str(config.get("owner")) != str(user_id):
            return None
        socket = config.get("socket")
        token_file = config.get("token_file")
        account = config.get("account", "main")
        if any(not isinstance(value, str) or not value for value in (socket, token_file, account)):
            raise GatewayError("worker_unavailable")
        token = Path(token_file)
        if not Path(socket).is_absolute() or not token.is_absolute():
            raise GatewayError("worker_unavailable")
        return cls(socket, token, account)


class Client:
    def __init__(self, endpoint, *, inline_image, on_queue=None, on_started=None):
        self.endpoint = endpoint
        self.inline_image = inline_image
        self.on_queue = on_queue
        self.on_started = on_started
        self.instructions = ""
        self.search = False
        self.images = False
        self.model = ""
        self.effort = ""

    async def new_session(self, *, instructions="", search=False, images=False):
        self.instructions, self.search, self.images = instructions, search, images

    async def set_model(self, _session, model, effort=None):
        self.model, self.effort = model, effort or ""

    async def prompt(self, text, on_text, *, media=(), on_image=None):
        operation = "image" if self.images else "transcribe" if media else "text"
        if text == "/usage" and not (self.images or self.search or media or self.model):
            operation = "usage"
        payload = {"account": {"mode": "operator", "name": self.endpoint.account},
                   "operation": operation}
        if operation != "usage":
            payload.update(
                prompt=text, model=self.model, effort=self.effort,
                instructions=self.instructions, search=self.search, media=list(media),
            )
        try:
            token = self.endpoint.token_file.read_text().strip()
            transport = httpx.AsyncHTTPTransport(uds=self.endpoint.socket)
            async with httpx.AsyncClient(transport=transport, timeout=1850) as client:
                async with client.stream(
                    "POST", "http://localhost/v1/turns",
                    json=payload, headers={"Authorization": f"Bearer {token}"},
                ) as response:
                    if response.is_error:
                        status = {401: "backend_auth", 403: "account_forbidden", 429: "queue_full"}
                        event = {"code": status.get(response.status_code, "worker_unavailable")}
                        await response.aread()
                        try:
                            body = response.json()
                        except ValueError:
                            body = None
                        if isinstance(body, dict) and body.get("type") == "error" and isinstance(body.get("code"), str):
                            event = body
                        raise _turn_error(response, event)
                    return await self.consume(response, on_text, on_image)
        except (OSError, httpx.HTTPError, ValueError, KeyError, TypeError):
            raise GatewayError("worker_unavailable") from None

    async def consume(self, response, on_text, on_image):
        result = {}
        async for line in response.aiter_lines():
            if len(line) > protocol.FRAME_LIMIT:
                raise GatewayError("worker_unavailable")
            event = json.loads(line)
            kind = event["type"]
            if kind == "queued" and self.on_queue is not None:
                self.on_queue(event["position"])
            elif kind == "started" and self.on_started is not None:
                self.on_started()
            elif kind == "text":
                await on_text(event["text"])
            elif kind == "image" and on_image is not None:
                image = self.inline_image(event.get("image"))
                if image is not None:
                    await on_image(*image)
            elif kind == "result":
                result = event["result"]
            elif kind == "error":
                raise _turn_error(response, event)
            elif kind == "done":
                return result
        raise GatewayError("worker_unavailable")
