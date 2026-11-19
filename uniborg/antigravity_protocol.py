"""Wire format and OAuth validation shared by the bot and worker."""

import json
import re
from dataclasses import dataclass, field
from hmac import compare_digest
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

VERSION = "1.5.0"
MAX_LINK_LENGTH = 8192
MAX_MEDIA_BYTES = 12 * 1024 * 1024
FRAME_LIMIT = MAX_MEDIA_BYTES * 4 // 3 + 262144
SOCKET_PATH = Path("/run/betterborg-antigravity.sock")
EGRESS_DIR = Path("/run/betterborg-antigravity-egress")
STATE_DIR = Path("/var/lib/betterborg-antigravity")
CONFIGURED = Path("/etc/betterborg-antigravity/enabled")
WORKDIR = "/tmp/work"
WORKER_VERSION = 1


async def send_control(writer, **message):
    writer.write(b"C" + json.dumps(message).encode() + b"\n")
    await writer.drain()


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
