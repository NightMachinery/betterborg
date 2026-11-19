"""Personal Google OAuth and chat through the pinned Antigravity ACP agent."""

import asyncio
import json
import os
import re
import tempfile
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field
from hmac import compare_digest
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

from uniborg import antigravity_network

VERSION = "1.5.0"
IMAGE = "python@sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b"
INSTALL = Path.home() / ".local/lib/betterborg/antigravity-acp" / VERSION
PROFILES = Path.home() / ".borg/antigravity"
WORKER_BUSY = (
    "Antigravity is already in use. Try again when the current operation ends."
)
CONNECT_NEEDED = "Connect your Google account in this bot’s DM with /antigravity_login."
UNAVAILABLE = "Antigravity is temporarily unavailable."
LOGIN_TIMEOUT = 300
MAX_LINK_LENGTH = 8192

# Fixed script. The validated return URL is sent on stdin, outside argv and logs.
CALLBACK_SCRIPT = """
import http.client
import json
import sys
port, target = json.load(sys.stdin)
connection = http.client.HTTPConnection("127.0.0.1", port, timeout=12)
connection.request("GET", target)
connection.getresponse().close()
connection.close()
"""


class AntigravityError(Exception):
    pass


def available():
    return all(
        os.access(INSTALL / name, os.X_OK)
        for name in (
            "agy_acp_server.par",
            "localharness_external",
        )
    )


def _profile(user_id):
    if type(user_id) is not int or user_id <= 0:
        raise ValueError("Missing Telegram user ID.")
    return PROFILES / str(user_id)


def linked(user_id):
    return (_profile(user_id) / "antigravity-acp" / "acp_token.json").is_file()


def _loopback(url):
    if not url or len(url) > MAX_LINK_LENGTH or not url.startswith("http://127.0.0.1:"):
        return None
    try:
        parts = urlsplit(url)
        if (
            not parts.port
            or parts.netloc != f"127.0.0.1:{parts.port}"
            or parts.path != "/"
            or parts.fragment
            or any(c.isspace() for c in url)
        ):
            return None
    except ValueError:
        return None
    return parts


def is_callback_url(text):
    parts = _loopback((text or "").strip())
    if not parts:
        return False
    query = parse_qs(parts.query)
    return bool(query.get("state") and (query.get("code") or query.get("error")))


@dataclass(frozen=True)
class LoginLink:
    url: str = field(repr=False)
    port: int
    state: str = field(repr=False)

    @classmethod
    def from_output(cls, line):
        match = re.search(
            r"https://accounts\.google\.com/o/oauth2/v2/auth\?[^\s]+", line
        )
        if not match or len(match[0]) > MAX_LINK_LENGTH:
            return None
        query = parse_qs(urlsplit(match[0]).query)
        (redirect,) = query.get("redirect_uri", [None])
        (state,) = query.get("state", [None])
        parts = _loopback(redirect)
        if parts and not parts.query and state and state.isascii():
            return cls(match[0], parts.port, state)
        return None

    def callback(self, text):
        parts = _loopback(text.strip())
        if not parts or parts.port != self.port:
            return None
        query = parse_qs(parts.query)
        states = query.get("state", [])
        if len(states) != 1 or not states[0].isascii():
            return None
        if not compare_digest(states[0], self.state) or not is_callback_url(text):
            return None
        return f"/?{parts.query}"


class Client:
    def __init__(self, process, container):
        self.process = process
        self.container = container
        self.reply_id = 0
        self.pending = None
        self.login_url = asyncio.get_running_loop().create_future()
        self.session_id = None
        self.search = False
        self.on_text = None

    async def send(self, **message):
        packet = json.dumps({"jsonrpc": "2.0", **message})
        self.process.stdin.write(packet.encode() + b"\n")
        await self.process.stdin.drain()

    async def request(self, method, *, timeout=35, **params):
        self.reply_id += 1
        reply = self.pending = asyncio.get_running_loop().create_future()
        try:
            await self.send(id=self.reply_id, method=method, params=params)
            async with asyncio.timeout(timeout):
                return await reply
        finally:
            self.pending = None

    async def drain_stderr(self):
        while line := await self.process.stderr.readline():
            if not self.login_url.done():
                try:
                    link = LoginLink.from_output(line.decode(errors="replace"))
                except ValueError:
                    continue
                if link:
                    self.login_url.set_result(link)

    async def read(self):
        try:
            while raw := await self.process.stdout.readline():
                try:
                    message = json.loads(raw)
                except ValueError:
                    continue
                await self.dispatch(message)
        finally:
            if self.pending is not None and not self.pending.done():
                self.pending.set_exception(AntigravityError(UNAVAILABLE))

    async def dispatch(self, message):
        method = message.get("method")
        if method and "id" in message:
            if method == "session/request_permission":
                outcome = self.permission(message.get("params") or {})
                await self.send(id=message["id"], result={"outcome": outcome})
            else:
                await self.send(
                    id=message["id"], error={"code": -32601, "message": "Unavailable"}
                )
            return
        if method == "session/update":
            params = message.get("params") or {}
            update = params.get("update") or {}
            content = update.get("content") or {}
            if (
                self.on_text
                and params.get("sessionId") == self.session_id
                and update.get("sessionUpdate") == "agent_message_chunk"
                and content.get("type") == "text"
            ):
                await self.on_text(content["text"])
            return
        if (
            message.get("id") == self.reply_id
            and self.pending
            and not self.pending.done()
        ):
            if "error" in message:
                error = message["error"] or {}
                note = CONNECT_NEEDED if error.get("code") == -32000 else UNAVAILABLE
                self.pending.set_exception(AntigravityError(note))
            else:
                self.pending.set_result(message.get("result") or {})

    def permission(self, params):
        call = params.get("toolCall") or {}
        # The pinned agent derives this title from the built-in name and
        # marks exec and MCP calls with different kinds.
        if (
            self.search
            and self.session_id
            and params.get("sessionId") == self.session_id
            and call.get("title") == "Run search_web?"
            and call.get("kind") == "search"
            and not call.get("locations")
            and not call.get("_meta")
        ):
            for option in params.get("options", []):
                if (
                    option.get("optionId") == "allow"
                    and option.get("kind") == "allow_once"
                ):
                    return {"outcome": "selected", "optionId": "allow"}
        return {"outcome": "cancelled"}

    async def initialize(self):
        result = await self.request(
            "initialize",
            protocolVersion=1,
            clientCapabilities={},
            clientInfo={"name": "Betterborg", "version": "1"},
        )
        agent = result.get("agentInfo") or {}
        if (
            result.get("protocolVersion") != 1
            or agent.get("name") != "antigravity-acp"
            or agent.get("version") != VERSION
        ):
            raise AntigravityError(UNAVAILABLE)

    async def new_session(self, *, instructions="", search=False):
        self.search = search
        meta = {"enabledTools": ["search_web"] if search else []}
        if instructions:
            meta["systemPrompt"] = {"append": instructions}
        session = await self.request(
            "session/new", cwd="/work", mcpServers=[], _meta={"antigravity": meta}
        )
        self.session_id = session["sessionId"]
        return session

    async def prompt(self, text, on_text):
        self.on_text = on_text
        return await self.request(
            "session/prompt",
            timeout=240,
            sessionId=self.session_id,
            prompt=[{"type": "text", "text": text}],
        )

    async def complete_login(self, link, target):
        process = await asyncio.create_subprocess_exec(
            "docker",
            "exec",
            "-i",
            self.container,
            "python",
            "-c",
            CALLBACK_SCRIPT,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        async with asyncio.timeout(15):
            await process.communicate(json.dumps([link.port, target]).encode())
        if process.returncode:
            raise AntigravityError(
                "Google sign-in failed. Please start again with /antigravity_login."
            )


@asynccontextmanager
async def _worker(user_id):
    if not available():
        raise AntigravityError(UNAVAILABLE)
    root = _profile(user_id)
    PROFILES.mkdir(mode=0o700, exist_ok=True, parents=True)
    root.mkdir(mode=0o700, exist_ok=True)
    session_store = "antigravity-acp/conversations"
    (root / session_store).mkdir(mode=0o700, exist_ok=True, parents=True)
    container = f"betterborg-agy-{uuid4().hex}"
    proxy = f"http://127.0.0.1:{antigravity_network.PROXY_PORT}"
    with tempfile.TemporaryDirectory(prefix="bb-agy-", dir="/tmp") as socket_dir:
        server = await asyncio.start_unix_server(
            antigravity_network.google_connect,
            f"{socket_dir}/connect",
            limit=4096,
            backlog=8,
        )
        async with server:
            process = await asyncio.create_subprocess_exec(
                "docker",
                "run",
                "--init",
                "--rm",
                "-i",
                "--pull=never",
                "--name",
                container,
                "--network=none",
                "--log-driver=none",
                "--read-only",
                "--cap-drop=ALL",
                "--security-opt=no-new-privileges",
                "--memory=800m",
                "--memory-swap=800m",
                "--cpus=1",
                "--pids-limit=80",
                "--user",
                f"{os.getuid()}:{os.getgid()}",
                "--tmpfs",
                "/tmp:rw,nosuid,nodev,size=320m,mode=1777",
                "--tmpfs",
                "/work:rw,nosuid,nodev,noexec,size=12m,mode=1777",
                "--tmpfs",
                f"/profile/{session_store}:rw,nosuid,nodev,noexec,size=48m,mode=1777",
                "--mount",
                f"type=bind,src={socket_dir},dst=/egress,readonly",
                "--mount",
                f"type=bind,src={INSTALL},dst=/opt/agy,readonly",
                "--mount",
                f"type=bind,src={root},dst=/profile",
                "--mount",
                f"type=bind,src={antigravity_network.__file__},dst=/egress.py,readonly",
                "--workdir",
                "/work",
                "--env",
                "GEMINI_HOME=/profile",
                "--env",
                "XDG_CACHE_HOME=/tmp",
                "--env",
                f"HTTPS_PROXY={proxy}",
                "--env",
                f"HTTP_PROXY={proxy}",
                "--env",
                f"grpc_proxy={proxy}",
                "--env",
                "NO_PROXY=127.0.0.1,localhost",
                IMAGE,
                "python",
                "/egress.py",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                limit=2**20,
            )
            client = Client(process, container)
            readers = [
                asyncio.create_task(client.read()),
                asyncio.create_task(client.drain_stderr()),
            ]
            try:
                await client.initialize()
                yield client
            except (OSError, TimeoutError, KeyError) as exc:
                raise AntigravityError(UNAVAILABLE) from exc
            finally:
                cleanup = await asyncio.create_subprocess_exec(
                    "docker",
                    "rm",
                    "-f",
                    container,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                with suppress(TimeoutError):
                    await asyncio.wait_for(cleanup.wait(), 10)
                if process.returncode is None:
                    with suppress(ProcessLookupError):
                        process.kill()
                for task in readers:
                    task.cancel()
                await asyncio.gather(*readers, return_exceptions=True)
                await process.wait()


@dataclass
class PendingLogin:
    link: asyncio.Future
    callback: asyncio.Future
    task: asyncio.Task | None = None


class Antigravity:
    def __init__(self):
        self.pending = {}
        self.active = asyncio.Lock()

    @asynccontextmanager
    async def worker(self, user_id):
        if self.active.locked():
            raise AntigravityError(WORKER_BUSY)
        try:
            async with self.active, _worker(user_id) as client:
                yield client
        except (OSError, TimeoutError) as exc:
            raise AntigravityError(UNAVAILABLE) from exc

    async def start_login(self, user_id):
        if linked(user_id):
            raise AntigravityError(
                "Already connected. Use /antigravity_logout to switch accounts."
            )
        loop = asyncio.get_running_loop()
        pending = PendingLogin(loop.create_future(), loop.create_future())
        if user_id in self.pending:
            raise AntigravityError(
                "A sign-in is in progress. Use the most recent Google sign-in link."
            )
        self.pending[user_id] = pending
        pending.task = asyncio.create_task(self._login(user_id, pending))
        return await asyncio.shield(pending.link)

    async def _login(self, user_id, pending):
        try:
            async with self.worker(user_id) as client, asyncio.timeout(LOGIN_TIMEOUT):
                auth = asyncio.create_task(
                    client.request(
                        "authenticate", methodId="oauth-personal", timeout=LOGIN_TIMEOUT
                    )
                )
                try:
                    done, _ = await asyncio.wait(
                        [auth, client.login_url], return_when=asyncio.FIRST_COMPLETED
                    )
                    if auth in done:
                        auth.result()
                        raise AntigravityError(
                            "Google sign-in did not provide a browser link."
                        )
                    link = client.login_url.result()
                    pending.link.set_result(link)
                    target = await pending.callback
                    await client.complete_login(link, target)
                    await auth
                    return linked(user_id)
                finally:
                    auth.cancel()
                    await asyncio.gather(auth, return_exceptions=True)
        except (AntigravityError, OSError, TimeoutError) as exc:
            if not pending.link.done():
                message = str(exc) if isinstance(exc, AntigravityError) else UNAVAILABLE
                pending.link.set_exception(AntigravityError(message))
            return False
        finally:
            if not pending.link.done():
                pending.link.cancel()
            self.pending.pop(user_id, None)

    async def finish_login(self, user_id, text):
        pending = self.pending.get(user_id)
        if not pending or not pending.link.done() or pending.callback.done():
            return False
        link = pending.link.result()
        target = link.callback(text)
        if not target:
            return False
        pending.callback.set_result(target)
        return await asyncio.shield(pending.task)

    async def logout(self, user_id):
        pending = self.pending.get(user_id)
        if pending:
            pending.task.cancel()
            await asyncio.gather(pending.task, return_exceptions=True)
        if linked(user_id):
            async with self.worker(user_id) as client:
                await client.request("logout")
        return not linked(user_id)


backend = Antigravity()


def select_model(session, model, effort=None):
    """Match the selected Gemini model against the signed-in agent's catalogue."""
    bare = model.removeprefix("gemini/")
    if bare.endswith("-latest"):
        family = bare.removeprefix("gemini-").removesuffix("-latest")
        if family == "pro":
            bare = "gemini-3.1-pro-preview"
        else:
            bare = "gemini-3.8-flash" if family == "flash" else bare
    options = (session.get("models") or {}).get("availableModels", [])
    ids = [option["modelId"] for option in options]
    if effort and f"{bare}-{effort}" in ids:
        return f"{bare}-{effort}"
    if bare in ids:
        return bare
    if f"{bare}-high" in ids:
        return f"{bare}-high"
    raise AntigravityError(
        f"{bare} is unavailable on this Google account. Choose another model with /setModel."
    )


def conversation_text(messages):
    """Preserve Betterborg's history selection and keep ACP slash commands out."""
    instructions, turns = [], []
    for message in messages:
        content = message.get("content")
        if isinstance(content, list):
            content = "\n".join(
                part["text"] for part in content if part.get("type") == "text"
            )
        if not isinstance(content, str) or not content:
            continue
        role = message.get("role")
        if role == "system":
            instructions.append(content)
        elif role in ("assistant", "user"):
            turns.append(f"{role.capitalize()}: {content}")
    return "\n\n".join(instructions), "\n\n".join(turns)
