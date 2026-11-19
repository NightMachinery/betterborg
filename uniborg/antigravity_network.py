"""HTTPS egress for the official ACP agent in a private network namespace."""

import asyncio
import contextlib
import ipaddress
import re
import socket
import sys

GOOGLE_ENDPOINTS = frozenset(
    {
        "accounts.google.com",
        "oauth2.googleapis.com",
        "www.googleapis.com",
        "cloudcode-pa.googleapis.com",
        "daily-cloudcode-pa.googleapis.com",
    }
)
PROXY_PORT = 17861
CONNECTIONS = asyncio.Semaphore(12)


async def relay(left, right):
    async def copy(reader, writer):
        while data := await reader.read(65536):
            writer.write(data)
            await writer.drain()

    tasks = [
        asyncio.create_task(copy(left[0], right[1])),
        asyncio.create_task(copy(right[0], left[1])),
    ]
    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def google_connect(reader, writer):
    if CONNECTIONS.locked():
        writer.close()
        return
    remote = None
    await CONNECTIONS.acquire()
    try:
        async with asyncio.timeout(10):
            header = await reader.readuntil(b"\r\n\r\n")
            request_line = header.split(b"\r\n", 1)[0]
            match = re.fullmatch(
                rb"CONNECT ([a-z0-9.-]+):443 HTTP/1\.[01]", request_line
            )
            host = match[1].decode() if match else None
            if host not in GOOGLE_ENDPOINTS:
                writer.write(b"HTTP/1.1 403 Forbidden\r\n\r\n")
                await writer.drain()
                return
            addresses = await asyncio.get_running_loop().getaddrinfo(
                host,
                443,
                type=socket.SOCK_STREAM,
                family=socket.AF_INET,
            )
            for address in addresses:
                ip = address[4][0]
                if not ipaddress.ip_address(ip).is_global:
                    continue
                try:
                    remote = await asyncio.open_connection(
                        ip, 443, family=socket.AF_INET
                    )
                except OSError:
                    continue
                break
            if remote is None:
                return
            writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            await writer.drain()
        async with asyncio.timeout(360):
            await relay((reader, writer), remote)
    except (OSError, EOFError, ValueError, asyncio.LimitOverrunError, TimeoutError):
        pass
    finally:
        CONNECTIONS.release()
        if remote:
            remote[1].close()
        writer.close()


async def loopback_proxy(path):
    """Start the agent's private loopback bridge to the host allowlist."""
    async def bridge(reader, writer):
        upstream = None
        try:
            upstream = await asyncio.open_unix_connection(path)
            await relay((reader, writer), upstream)
        except OSError:
            pass
        finally:
            if upstream:
                upstream[1].close()
            writer.close()

    return await asyncio.start_server(bridge, "127.0.0.1", PROXY_PORT)


async def worker():
    async with await loopback_proxy("/egress/connect"):
        agent = await asyncio.create_subprocess_exec(
            "/opt/agy/agy_acp_server.par",
            "--uid=",
        )
        try:
            async with asyncio.timeout(360):
                return await agent.wait()
        except TimeoutError:
            return 1  # Exiting the container also stops its agent and harness.


if __name__ == "__main__":
    with contextlib.suppress(KeyboardInterrupt):
        sys.exit(asyncio.run(worker()))
