"""The media store on disk (`uniborg/media_store.py`)."""

import asyncio
import base64
from pathlib import Path
import tempfile
import unittest

from uniborg import media_store
from uniborg.media_store import MediaInfo

DAY = media_store.DAY_SECONDS


def _photo(data: bytes, name="a.png") -> MediaInfo:
    return MediaInfo(
        storage_type=media_store.STORAGE_BASE64,
        data=base64.b64encode(data).decode("ascii"),
        filename=name,
        mime_type="image/png",
    )


class MediaStoreTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "nested" / "media.sqlite3"
        self.now = [1000.0]

    def store(self, **kwargs):
        return media_store.MediaStore(self.path, clock=lambda: self.now[0], **kwargs)

    def test_text_and_binary_files_come_back_as_stored(self):
        store = self.store()
        text = MediaInfo(storage_type=media_store.STORAGE_TEXT, data="héllo")
        photo = _photo(b"\x89PNG\x00\xff")

        async def run():
            await store.put("t", text)
            await store.put("p", photo)
            return await store.get("t"), await store.get("p"), await store.get("x")

        self.assertEqual(asyncio.run(run()), (text, photo, None))

    def test_another_store_on_the_same_file_sees_the_files(self):
        asyncio.run(self.store().put("p", _photo(b"x")))

        self.assertEqual(asyncio.run(self.store().get("p")), _photo(b"x"))

    def test_a_file_expires_a_ttl_after_its_last_use(self):
        store = self.store(ttl_seconds=7 * DAY)

        async def run():
            await store.put("p", _photo(b"x"))
            self.now[0] += 6 * DAY
            used = await store.get("p")
            self.now[0] += 6 * DAY
            renewed = await store.get("p")
            self.now[0] += 7 * DAY
            return used, renewed, await store.get("p")

        used, renewed, expired = asyncio.run(run())
        self.assertIsNotNone(used)
        self.assertIsNotNone(renewed)
        self.assertIsNone(expired)

    def test_cleanup_drops_only_expired_files(self):
        store = self.store(ttl_seconds=DAY)

        async def run():
            await store.put("old", _photo(b"o"))
            self.now[0] += 2 * DAY
            await store.put("new", _photo(b"n"))
            dropped = await store.cleanup()
            self.now[0] -= 2 * DAY
            return dropped, await store.get("old"), await store.get("new")

        self.assertEqual(asyncio.run(run()), (1, None, _photo(b"n")))

    def test_a_file_over_the_cap_is_refused(self):
        store = self.store(max_file_bytes=4)

        async def run():
            return await store.put("big", _photo(b"12345")), await store.get("big")

        self.assertEqual(asyncio.run(run()), (False, None))

    def test_the_least_recently_used_go_when_the_total_is_over(self):
        store = self.store(max_total_bytes=12)

        async def run():
            for key in ("a", "b", "c"):
                await store.put(key, _photo(b"1234"))
                self.now[0] += 1
            await store.get("a")
            await store.put("d", _photo(b"1234"))
            return [key for key in "abcd" if await store.get(key)]

        self.assertEqual(asyncio.run(run()), ["a", "c", "d"])

    def test_an_unusable_path_fails_softly(self):
        self.path.parent.mkdir(parents=True)
        self.path.mkdir()
        store = self.store()

        async def run():
            return (
                await store.put("p", _photo(b"x")),
                await store.get("p"),
                await store.cleanup(),
            )

        with self.assertLogs(media_store._log, "WARNING"):
            self.assertEqual(asyncio.run(run()), (False, None, 0))


if __name__ == "__main__":
    unittest.main()
