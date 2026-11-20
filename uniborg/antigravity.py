"""Personal Google OAuth and chat through the pinned Antigravity ACP agent."""

import asyncio
import base64
import binascii
import fcntl
import json
import os
import tempfile
from contextlib import AsyncExitStack, asynccontextmanager, contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from uniborg import antigravity_gateway
from uniborg import antigravity_network, antigravity_protocol as protocol
from uniborg.antigravity_protocol import LoginLink, is_callback_url

VERSION = protocol.VERSION
IMAGE = "python@sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b"
INSTALL = Path.home() / ".local/lib/betterborg/antigravity-acp" / VERSION
PROFILES = Path.home() / ".borg/antigravity"
WORKER_BUSY = (
    "Antigravity is already in use. Try again when the current operation ends."
)
WORKER_WAIT_TIMEOUT = 15 * 60
WORKER_POLL_INTERVAL = 0.5
CONNECT_NEEDED = "Connect your Google account in this bot’s DM with /antigravity_login."
SHARED_CONNECT_NEEDED = (
    "Run /antigravity_login in the Chat bot, then choose Antigravity here."
)
UNAVAILABLE = "Antigravity is temporarily unavailable."
LOGIN_TIMEOUT = 300
MAX_MEDIA_BYTES = protocol.MAX_MEDIA_BYTES
IMAGE_TYPES = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp"}

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


class WorkerBusy(AntigravityError):
    pass


def available():
    if protocol.CONFIGURED.is_file():
        return protocol.SOCKET_PATH.is_socket()
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
    profile = _profile(user_id)
    if protocol.CONFIGURED.is_file():
        return (protocol.STATE_DIR / "linked" / profile.name).is_file()
    return (profile / "antigravity-acp" / "acp_token.json").is_file()


def private_admin(event):
    from uniborg.util import is_admin_by_id

    user_id = getattr(event, "sender_id", None)
    return bool(
        getattr(event, "is_private", False)
        and not getattr(event, "out", False)
        and type(user_id) is int
        and user_id > 0
        and getattr(event, "chat_id", None) == user_id
        and is_admin_by_id(user_id)
    )


@contextmanager
def _worker_slot():
    PROFILES.mkdir(mode=0o700, exist_ok=True, parents=True)
    fd = os.open(
        PROFILES / "worker.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600
    )
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise WorkerBusy(WORKER_BUSY) from None
        yield
    finally:
        os.close(fd)


def inline_image(block):
    if not isinstance(block, dict) or block.get("type") != "image":
        return None
    mime, encoded = block.get("mimeType"), block.get("data")
    if (
        mime not in IMAGE_TYPES
        or not isinstance(encoded, str)
        or len(encoded) > MAX_MEDIA_BYTES * 4 // 3
    ):
        return None
    try:
        data = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error):
        return None
    signatures = {
        "image/jpeg": b"\xff\xd8\xff",
        "image/png": b"\x89PNG\r\n\x1a\n",
        "image/webp": b"RIFF",
    }
    if not data.startswith(signatures[mime]) or (
        mime == "image/webp" and data[8:12] != b"WEBP"
    ):
        return None
    return data, IMAGE_TYPES[mime]


class Client:
    workdir = "/work"

    def __init__(self, process, container):
        self.process = process
        self.container = container
        self.reply_id = 0
        self.pending = None
        self.login_url = asyncio.get_running_loop().create_future()
        self.session_id = None
        self.search = False
        self.images = False
        self.on_text = None
        self.on_image = None

    async def send(self, **message):
        packet = json.dumps({"jsonrpc": "2.0", **message})
        await self.write_packet(packet.encode() + b"\n")

    async def write_packet(self, raw):
        self.process.stdin.write(raw)
        await self.process.stdin.drain()

    async def read_packet(self):
        return await self.process.stdout.readline()

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
            while raw := await self.read_packet():
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
            if self.session_id is None or params.get("sessionId") != self.session_id:
                return
            update = params.get("update") or {}
            content = update.get("content") or {}
            if (
                self.on_text
                and update.get("sessionUpdate") == "agent_message_chunk"
                and isinstance(content, dict)
                and content.get("type") == "text"
            ):
                await self.on_text(content["text"])
            if (
                self.on_image
                and self.images
                and update.get("sessionUpdate") == "tool_call_update"
                and update.get("status") == "completed"
                and isinstance(content, list)
            ):
                for item in content:
                    if not isinstance(item, dict):
                        continue
                    block = item.get("content", item)
                    image = inline_image(block)
                    if image is not None:
                        await self.on_image(*image)
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
        allowed = (
            self.search
            and call.get("title") == "Run search_web?"
            and call.get("kind") == "search"
        ) or (
            self.images
            and call.get("title") == "Run generate_image?"
            and call.get("kind") == "other"
        )
        if (
            allowed
            and self.session_id
            and params.get("sessionId") == self.session_id
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

    async def new_session(self, *, instructions="", search=False, images=False):
        self.search = search
        self.images = images
        tools = [
            name
            for name, enabled in (("search_web", search), ("generate_image", images))
            if enabled
        ]
        meta = {"enabledTools": tools}
        if instructions:
            meta["systemPrompt"] = {"append": instructions}
        session = await self.request(
            "session/new", cwd=self.workdir, mcpServers=[], _meta={"antigravity": meta}
        )
        self.session_id = session["sessionId"]
        return session

    async def prompt(self, text, on_text, *, media=(), on_image=None):
        self.on_text = on_text
        self.on_image = on_image
        return await self.request(
            "session/prompt",
            timeout=240,
            sessionId=self.session_id,
            prompt=[{"type": "text", "text": text}, *media],
        )

    async def set_model(self, session, model, effort=None):
        model_id = select_model(session, model, effort)
        if model_id != (session.get("models") or {}).get("currentModelId"):
            await self.request(
                "session/set_model", sessionId=self.session_id, modelId=model_id
            )
        return model_id

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


class SystemdClient(Client):
    workdir = protocol.WORKDIR

    def __init__(self, reader, writer):
        super().__init__(None, None)
        self.reader = reader
        self.writer = writer
        self.callback_result = None

    async def write_packet(self, raw):
        self.writer.write(b"A" + raw)
        await self.writer.drain()

    async def read_packet(self):
        while raw := await self.reader.readline():
            if raw.startswith(b"A"):
                return raw[1:]
            if raw.startswith(b"C") and len(raw) <= protocol.MAX_LINK_LENGTH * 2:
                try:
                    control = json.loads(raw[1:])
                    if control.get("kind") == "login" and not self.login_url.done():
                        link = LoginLink.from_output(control["url"])
                        if link:
                            self.login_url.set_result(link)
                    if control.get("kind") == "callback" and self.callback_result is not None:
                        if not self.callback_result.done():
                            self.callback_result.set_result(control.get("ok") is True)
                except (AttributeError, KeyError, TypeError, ValueError):
                    continue
        return b""

    async def complete_login(self, link, target):
        self.callback_result = asyncio.get_running_loop().create_future()
        async with asyncio.timeout(15):
            await protocol.send_control(
                self.writer, kind="callback", url=f"http://127.0.0.1:{link.port}{target}"
            )
            ok = await self.callback_result
        if not ok:
            raise AntigravityError(
                "Google sign-in failed. Please start again with /antigravity_login."
            )


@asynccontextmanager
async def _systemd_worker(user_id):
    socket_name = uuid4().hex
    egress_path = protocol.EGRESS_DIR / socket_name
    server = await asyncio.start_unix_server(
        antigravity_network.google_connect, egress_path, limit=4096, backlog=8
    )
    try:
        async with server:
            os.chmod(egress_path, 0o660)
            reader, writer = await asyncio.open_unix_connection(
                protocol.SOCKET_PATH, limit=protocol.FRAME_LIMIT
            )
            client = SystemdClient(reader, writer)
            reads = asyncio.create_task(client.read())
            try:
                await protocol.send_control(
                    writer, kind="start", version=protocol.WORKER_VERSION,
                    owner=user_id, egress=socket_name,
                )
                await client.initialize()
                yield client
            finally:
                with suppress(OSError, TimeoutError, ValueError):
                    writer.write_eof()
                    await asyncio.wait_for(reads, 5)
                reads.cancel()
                await asyncio.gather(reads, return_exceptions=True)
                writer.close()
                with suppress(OSError):
                    await writer.wait_closed()
    finally:
        egress_path.unlink(missing_ok=True)


def _worker(user_id):
    if protocol.CONFIGURED.is_file():
        return _systemd_worker(user_id)
    return _docker_worker(user_id)


@asynccontextmanager
async def _docker_worker(user_id):
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
                limit=protocol.FRAME_LIMIT,
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

    @asynccontextmanager
    async def worker(self, user_id, *, wait=False, on_wait=None):
        try:
            async with AsyncExitStack() as stack:
                try:
                    async with asyncio.timeout(WORKER_WAIT_TIMEOUT):
                        while True:
                            try:
                                stack.enter_context(_worker_slot())
                            except WorkerBusy:
                                if not wait:
                                    raise
                                if on_wait is not None:
                                    on_wait()
                                    on_wait = None
                                await asyncio.sleep(WORKER_POLL_INTERVAL)
                            else:
                                break
                except TimeoutError:
                    raise AntigravityError(
                        "Antigravity stayed busy throughout the wait. Please send the media again."
                    ) from None
                async with _worker(user_id) as client:
                    yield client
        except (OSError, TimeoutError) as exc:
            raise AntigravityError(UNAVAILABLE) from exc

    @asynccontextmanager
    async def turn_worker(
        self, user_id, *, wait=False, on_wait=None, on_queue=None, on_started=None
    ):
        try:
            endpoint = antigravity_gateway.OperatorEndpoint.for_user(user_id)
            if endpoint is None:
                async with self.worker(user_id, wait=wait, on_wait=on_wait) as client:
                    if on_started is not None:
                        on_started()
                    yield client
                return
            yield antigravity_gateway.Client(
                endpoint, inline_image=inline_image,
                on_queue=on_queue, on_started=on_started,
            )
        except antigravity_gateway.GatewayError as exc:
            messages = {
                "login_required": SHARED_CONNECT_NEEDED,
                "worker_busy": WORKER_BUSY,
                "queue_full": "The Antigravity queue is full. Try again in a moment.",
                "queue_timeout": "Antigravity stayed busy throughout the wait. Try again.",
                "run_timeout": "Antigravity ran out of time on this request.",
                "model_unavailable": "That model is unavailable on this Google account.",
                "no_image": "Antigravity returned no image. Try another prompt or check /antigravity_usage.",
                "request_too_large": "This request is too large for Antigravity.",
            }
            raise AntigravityError(messages.get(exc.code, UNAVAILABLE)) from None

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

    def accept_callback(self, user_id, text):
        pending = self.pending.get(user_id)
        if not pending or not pending.link.done() or pending.callback.done():
            return None
        link = pending.link.result()
        target = link.callback(text)
        if not target:
            return None
        pending.callback.set_result(target)
        return pending.task

    async def finish_login(self, user_id, text):
        task = self.accept_callback(user_id, text)
        return await asyncio.shield(task) if task is not None else False

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


async def generate_images(user_id, prompt, *, number=1, aspect_ratio="1:1"):
    """Request images from the official tool and collect only inline ACP output."""
    from uniborg.constants import GEMINI_FLASH_LATEST

    images = []

    async def collect_image(data, extension):
        image = (data, extension)
        if image not in images and len(images) < number:
            images.append(image)

    async def ignore_text(_text):
        pass

    async with backend.turn_worker(user_id) as client:
        session = await client.new_session(
            images=True,
            instructions="Use the generate_image tool to deliver the requested images.",
        )
        await client.set_model(session, GEMINI_FLASH_LATEST)
        await client.prompt(
            f"Generate {number} image(s) with aspect ratio {aspect_ratio}.\n\nImage prompt: {prompt}",
            ignore_text,
            on_image=collect_image,
        )
    if not images:
        raise AntigravityError(
            "Antigravity returned no image. Try another prompt or check /antigravity_usage in the Chat bot."
        )
    return images


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
