"""Files kept on disk for a while, for messages the bot cannot download again.

A guest message's file reference cannot be refreshed (docs/guest_mode.md), so
the chat bot keeps the files of guest messages here, for the replies that
continue their exchange. Each file expires `ttl_seconds` after it was last
stored or read; the oldest go first when the store passes `max_total_bytes`.

One SQLite file holds everything. Calls run in a worker thread, each on a
connection of its own, so several processes may share the file.
"""

import asyncio
import base64
from dataclasses import dataclass
import logging
import os
from pathlib import Path
import sqlite3
import time
from typing import Callable, Optional

_log = logging.getLogger(__name__)

DEFAULT_PATH = Path(
    os.environ.get("borg_media_store_path")
    or os.path.expanduser("~/.borg/media_store.sqlite3")
)
DAY_SECONDS = 24 * 60 * 60

#: How a file's `data` is held: decoded text, or Base64 of its bytes.
STORAGE_TEXT = "text"
STORAGE_BASE64 = "base64"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS media (
    key TEXT PRIMARY KEY,
    storage_type TEXT NOT NULL,
    data BLOB NOT NULL,
    filename TEXT,
    mime_type TEXT,
    size INTEGER NOT NULL,
    used_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS media_used_at ON media (used_at);
"""


@dataclass(frozen=True)
class MediaInfo:
    """A file as the chat bot's media cache holds it."""

    storage_type: str
    #: The text, or the Base64 of the bytes, by `storage_type`.
    data: str
    filename: Optional[str] = None
    mime_type: Optional[str] = None


def _to_blob(info: MediaInfo) -> bytes:
    if info.storage_type == STORAGE_TEXT:
        return info.data.encode("utf-8")
    elif info.storage_type == STORAGE_BASE64:
        #: The bytes themselves: a third smaller than their Base64.
        return base64.b64decode(info.data)
    else:
        raise ValueError(f"Unknown storage type: {info.storage_type!r}")


def _from_blob(storage_type: str, blob: bytes) -> str:
    if storage_type == STORAGE_TEXT:
        return blob.decode("utf-8")
    elif storage_type == STORAGE_BASE64:
        return base64.b64encode(blob).decode("ascii")
    else:
        raise ValueError(f"Unknown storage type: {storage_type!r}")


class MediaStore:
    """Files by key, on disk, each kept `ttl_seconds` after its last use.

    `put` refuses a file over `max_file_bytes`, then drops the least recently
    used files while the total is over `max_total_bytes`. `cleanup` drops
    the expired ones; `get` never returns one.
    """

    def __init__(
        self,
        path: Optional[Path] = None,
        *,
        ttl_seconds: float = 7 * DAY_SECONDS,
        max_file_bytes: int = 20 * 2**20,
        max_total_bytes: int = 2**30,
        clock: Callable[[], float] = time.time,
        logger: Optional[logging.Logger] = None,
    ):
        self.path = Path(path) if path is not None else DEFAULT_PATH
        self._ttl_seconds = ttl_seconds
        self._max_file_bytes = max_file_bytes
        self._max_total_bytes = max_total_bytes
        self._clock = clock
        self._log = logger or _log

    def _connect(self) -> sqlite3.Connection:
        #: Every time, so a file deleted under a running bot comes back.
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=30)
        connection.executescript(_SCHEMA)
        return connection

    def _run(self, work: Callable[[sqlite3.Connection], object]) -> object:
        connection = self._connect()
        try:
            with connection:
                return work(connection)
        finally:
            connection.close()

    def _put(self, key: str, info: MediaInfo) -> bool:
        blob = _to_blob(info)
        if len(blob) > self._max_file_bytes:
            return False

        def work(connection):
            connection.execute(
                "INSERT OR REPLACE INTO media"
                " (key, storage_type, data, filename, mime_type, size, used_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    key,
                    info.storage_type,
                    blob,
                    info.filename,
                    info.mime_type,
                    len(blob),
                    self._clock(),
                ),
            )
            (total,) = connection.execute(
                "SELECT COALESCE(SUM(size), 0) FROM media"
            ).fetchone()
            rows = connection.execute(
                "SELECT key, size FROM media WHERE key != ? ORDER BY used_at",
                (key,),
            )
            dropped = []
            for old_key, size in rows:
                if total <= self._max_total_bytes:
                    break
                dropped.append((old_key,))
                total -= size
            connection.executemany("DELETE FROM media WHERE key = ?", dropped)
            return True

        return self._run(work)

    def _get(self, key: str) -> Optional[MediaInfo]:
        def work(connection):
            now = self._clock()
            row = connection.execute(
                "SELECT storage_type, data, filename, mime_type FROM media"
                " WHERE key = ? AND used_at > ?",
                (key, now - self._ttl_seconds),
            ).fetchone()
            if row is None:
                return None
            connection.execute("UPDATE media SET used_at = ? WHERE key = ?", (now, key))
            storage_type, blob, filename, mime_type = row
            return MediaInfo(
                storage_type=storage_type,
                data=_from_blob(storage_type, blob),
                filename=filename,
                mime_type=mime_type,
            )

        return self._run(work)

    def _cleanup(self) -> int:
        def work(connection):
            return connection.execute(
                "DELETE FROM media WHERE used_at <= ?",
                (self._clock() - self._ttl_seconds,),
            ).rowcount

        return self._run(work)

    async def put(self, key: str, info: MediaInfo) -> bool:
        """Stores INFO under KEY; False when it is too big or the disk fails."""
        try:
            return await asyncio.to_thread(self._put, key, info)
        except Exception:
            self._log.warning("Could not store media %s", key, exc_info=True)
            return False

    async def get(self, key: str) -> Optional[MediaInfo]:
        """The file under KEY, renewing it; None when absent, expired or unreadable."""
        try:
            return await asyncio.to_thread(self._get, key)
        except Exception:
            self._log.warning("Could not read media %s", key, exc_info=True)
            return None

    async def cleanup(self) -> int:
        """Drops the expired files; returns how many."""
        try:
            return await asyncio.to_thread(self._cleanup)
        except Exception:
            self._log.warning("Could not clean up the media store", exc_info=True)
            return 0

    async def cleanup_forever(self, *, interval_seconds: float = 60 * 60) -> None:
        """Runs `cleanup` now and then every INTERVAL_SECONDS, until cancelled."""
        while True:
            dropped = await self.cleanup()
            if dropped:
                self._log.info("Media store: dropped %d expired files", dropped)
            await asyncio.sleep(interval_seconds)
