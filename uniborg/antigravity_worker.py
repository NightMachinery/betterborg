"""Serve one ACP connection inside the dedicated systemd sandbox."""

import asyncio
import ctypes
import http.client
import json
import os
import re
import signal
import socket
import stat
import sys
from contextlib import suppress
from pathlib import Path

if __package__:
    from . import antigravity_network as network, antigravity_protocol as protocol
else:
    import antigravity_network as network
    import antigravity_protocol as protocol

INSTALL = Path(__file__).resolve().parent / "agent"
SCRATCH = Path("/tmp")
NATIVE = "agy_acp_server.par"


def restrict_files(profile):
    """Require Landlock and expose only this account's profile to the native process."""
    if os.uname().machine != "x86_64":
        raise RuntimeError("Unsupported worker architecture")

    class PathRule(ctypes.Structure):
        _pack_ = 1
        _fields_ = [("access", ctypes.c_uint64), ("fd", ctypes.c_int32)]

    libc = ctypes.CDLL(None, use_errno=True)
    libc.syscall.restype = ctypes.c_long

    def syscall(number, *args):
        result = libc.syscall(number, *args)
        if result < 0:
            error = ctypes.get_errno()
            raise OSError(error, os.strerror(error))
        return result

    # ABI 3 added truncate protection. A missing feature must stop the worker.
    if syscall(444, 0, 0, 1) < 3:
        raise RuntimeError("Landlock ABI 3 or newer is required")
    all_fs = (1 << 15) - 1
    rights = ctypes.c_uint64(all_fs)
    rules = syscall(444, ctypes.byref(rights), ctypes.sizeof(rights), 0)
    execute, write, read_file, read_dir, truncate = 1, 2, 4, 8, 1 << 14
    read_tree = read_file | read_dir
    paths = (
        ("/usr", read_tree | execute),
        (INSTALL, read_tree | execute),
        (profile, all_fs & ~execute),
        (SCRATCH, all_fs),
        ("/dev/null", read_file | write | truncate),
        ("/dev/random", read_file),
        ("/dev/urandom", read_file),
        ("/etc/group", read_file),
        ("/etc/ld.so.cache", read_file),
        ("/etc/nsswitch.conf", read_file),
        ("/etc/passwd", read_file),
        ("/etc/ssl/certs", read_tree),
        ("/proc/cpuinfo", read_file),
        ("/proc/meminfo", read_file),
        ("/sys/devices/system/cpu", read_tree),
    )
    try:
        for path, access in paths:
            fd = os.open(path, os.O_PATH | os.O_CLOEXEC)
            try:
                syscall(445, rules, 1, ctypes.byref(PathRule(access, fd)), 0)
            finally:
                os.close(fd)
        if libc.prctl(38, 1, 0, 0, 0):
            raise RuntimeError("Failed to set no_new_privs")
        syscall(446, rules, 0)
    finally:
        os.close(rules)


def prepare_profile(owner):
    profile = protocol.STATE_DIR / "profiles" / str(owner)
    for path in (profile, profile / "antigravity-acp"):
        path.mkdir(exist_ok=True, mode=0o700)
        if path.is_symlink() or not path.is_dir():
            raise ValueError("Invalid worker profile")
    for path in ("conversations", "work", "cache"):
        (SCRATCH / path).mkdir(mode=0o700)
    store = profile / "antigravity-acp" / "conversations"
    if not store.is_symlink():
        store.symlink_to(SCRATCH / "conversations")
    if store.readlink() != SCRATCH / "conversations":
        raise ValueError("Invalid worker session store")
    return profile


def publish_status(profile):
    marker = protocol.STATE_DIR / "linked" / profile.name
    try:
        folder = os.open(profile / "antigravity-acp", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            token = os.stat("acp_token.json", dir_fd=folder, follow_symlinks=False)
        finally:
            os.close(folder)
        connected = stat.S_ISREG(token.st_mode) and token.st_size > 0
    except FileNotFoundError:
        connected = False
    if connected:
        with suppress(FileExistsError):
            os.close(os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0))
    else:
        marker.unlink(missing_ok=True)


async def login_callback(link, url):
    target = link.callback(url)

    def deliver():
        connection = http.client.HTTPConnection("127.0.0.1", link.port, timeout=10)
        try:
            connection.request("GET", target)
            connection.getresponse().close()
        finally:
            connection.close()

    if target is None:
        return False
    try:
        await asyncio.to_thread(deliver)
    except (OSError, http.client.HTTPException):
        return False
    return True


async def serve(reader, writer):
    async with asyncio.timeout(5):
        raw = await reader.readuntil(b"\n")
    if not raw.startswith(b"C") or len(raw) > 512:
        raise ValueError("Invalid worker preamble")
    start = json.loads(raw[1:])
    owner, egress = start["owner"], start["egress"]
    if (
        start.get("kind") != "start"
        or start.get("version") != protocol.WORKER_VERSION
        or type(owner) is not int
        or not 0 < owner < 2**63
        or not isinstance(egress, str)
        or not re.fullmatch("[0-9a-f]{32}", egress)
    ):
        raise ValueError("Invalid worker preamble")

    profile = prepare_profile(owner)
    publish_status(profile)
    proxy = f"http://127.0.0.1:{network.PROXY_PORT}"
    link = None
    callback_used = False
    account_calls = set()
    async with await network.loopback_proxy(protocol.EGRESS_DIR / egress):
        agent = await asyncio.create_subprocess_exec(
            sys.executable, "-EBs", str(Path(__file__).resolve()), "--agent", str(profile),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
            limit=protocol.FRAME_LIMIT,
            cwd=protocol.WORKDIR,
            env={
                "PATH": "/usr/bin:/bin",
                "LANG": "C.UTF-8",
                "GEMINI_HOME": str(profile),
                "TMPDIR": str(SCRATCH),
                "XDG_CACHE_HOME": str(SCRATCH / "cache"),
                "HTTP_PROXY": proxy,
                "HTTPS_PROXY": proxy,
                "grpc_proxy": proxy,
                "NO_PROXY": "127.0.0.1,localhost",
            },
        )

        async def to_agent():
            nonlocal callback_used
            while raw := await reader.readline():
                if raw.startswith(b"A"):
                    packet = raw[1:]
                    if len(packet) < 4096:
                        with suppress(ValueError, TypeError, AttributeError):
                            msg = json.loads(packet)
                            if msg.get("method") in ("authenticate", "logout"):
                                account_calls.add(msg.get("id"))
                    agent.stdin.write(packet)
                    await agent.stdin.drain()
                elif raw.startswith(b"C") and len(raw) <= protocol.MAX_LINK_LENGTH * 2:
                    request = json.loads(raw[1:])
                    if request.get("kind") != "callback":
                        raise ValueError("Unknown worker command")
                    url = request.get("url")
                    ok = False
                    if link and not callback_used and isinstance(url, str) and link.callback(url):
                        callback_used = True
                        ok = await login_callback(link, url)
                    await protocol.send_control(writer, kind="callback", ok=ok)
                else:
                    raise ValueError("Invalid worker frame")

        async def to_bot():
            while raw := await agent.stdout.readline():
                if account_calls and len(raw) < 4096:
                    with suppress(ValueError, TypeError, AttributeError):
                        request_id = json.loads(raw).get("id")
                        if request_id in account_calls:
                            publish_status(profile)
                            account_calls.discard(request_id)
                writer.write(b"A" + raw)
                await writer.drain()

        async def read_login_link():
            nonlocal link
            while raw := await agent.stderr.readline():
                if link is None:
                    with suppress(ValueError):
                        link = protocol.LoginLink.from_output(raw.decode(errors="replace"))
                        if link:
                            await protocol.send_control(writer, kind="login", url=link.url)

        transfers = [asyncio.create_task(coro()) for coro in (to_agent, to_bot, read_login_link)]
        try:
            done, _ = await asyncio.wait(transfers[:2], return_when=asyncio.FIRST_COMPLETED)
            for transfer in done:
                transfer.result()
        finally:
            for transfer in transfers:
                transfer.cancel()
            with suppress(ProcessLookupError):
                os.killpg(agent.pid, signal.SIGKILL)
            await asyncio.gather(*transfers, return_exceptions=True)
            with suppress(TimeoutError):
                await asyncio.wait_for(agent.wait(), 2)
            publish_status(profile)


async def main():
    if {name for _, name in socket.if_nameindex()} != {"lo"}:
        raise RuntimeError("Private networking is required")
    connection = socket.socket(fileno=os.dup(0))
    reader, writer = await asyncio.open_unix_connection(
        sock=connection, limit=protocol.FRAME_LIMIT
    )
    try:
        async with asyncio.timeout(360):
            await serve(reader, writer)
    finally:
        writer.close()
        with suppress(OSError):
            await writer.wait_closed()


async def check_installed_socket():
    """Run as the bot Unix account before enabling the installed backend."""
    reader, writer = await asyncio.open_unix_connection(
        protocol.SOCKET_PATH, limit=protocol.FRAME_LIMIT
    )
    try:
        async with asyncio.timeout(45):
            await protocol.send_control(
                writer, kind="start", version=protocol.WORKER_VERSION,
                owner=2**63 - 1, egress="0" * 32,
            )
            request = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": 1,
                    "clientCapabilities": {},
                    "clientInfo": {"name": "Betterborg", "version": "1"},
                },
            }
            writer.write(b"A" + json.dumps(request).encode() + b"\n")
            await writer.drain()
            raw = await reader.readline()
            if not raw.startswith(b"A"):
                raise RuntimeError("No ACP response from the installed worker")
            result = json.loads(raw[1:])["result"]
            if (
                result["protocolVersion"] != 1
                or result["agentInfo"]["name"] != "antigravity-acp"
                or result["agentInfo"]["version"] != protocol.VERSION
            ):
                raise RuntimeError("Unexpected ACP worker version")
            writer.write_eof()
            if await reader.read(1):
                raise RuntimeError("The installed worker did not close after disconnect")
    finally:
        writer.close()
        await writer.wait_closed()


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--agent":
        restrict_files(Path(sys.argv[2]))
        os.execv(INSTALL / NATIVE, [str(INSTALL / NATIVE), "--uid="])
    elif sys.argv[1:] == ["--check-installed-socket"]:
        asyncio.run(check_installed_socket())
    else:
        asyncio.run(main())
