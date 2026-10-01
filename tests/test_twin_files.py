"""`history_util.is_twin_file`: which messages count as twins (docs/twin_files.md)."""

from datetime import datetime, timezone
import unittest

from telethon.tl.types import (
    Document,
    DocumentAttributeFilename,
    Message,
    MessageFwdHeader,
    MessageMediaDocument,
    PeerUser,
)

from uniborg import history_util
from uniborg.constants import BOT_META_INFO_PREFIX, TWIN_FILE_MARKER

BOT_ID = 999000772
USER_ID = 999000771
T0 = datetime(2026, 10, 1, tzinfo=timezone.utc)


def _document():
    return MessageMediaDocument(
        document=Document(
            id=1,
            access_hash=1,
            file_reference=b"",
            date=T0,
            mime_type="text/markdown",
            size=3,
            dc_id=2,
            attributes=[DocumentAttributeFilename("answer.md")],
        )
    )


def _message(
    caption=f"{TWIN_FILE_MARKER}**Title**",
    *,
    sender=BOT_ID,
    out=True,
    media=True,
    forwarded=False,
):
    return Message(
        id=10,
        peer_id=PeerUser(USER_ID),
        date=T0,
        message=caption,
        out=out,
        from_id=PeerUser(sender) if sender is not None else None,
        media=_document() if media else None,
        fwd_from=MessageFwdHeader(date=T0) if forwarded else None,
    )


def _twin(message):
    return history_util.is_twin_file(message, self_id=BOT_ID)


class IsTwinFileTests(unittest.TestCase):
    def test_a_marked_file_we_sent_is_a_twin(self):
        self.assertTrue(_twin(_message()))

    def test_an_outgoing_private_message_without_a_sender_counts_as_ours(self):
        self.assertTrue(_twin(_message(sender=None)))

    def test_a_forwarded_twin_is_kept(self):
        self.assertFalse(_twin(_message(forwarded=True)))
        self.assertFalse(_twin(_message(sender=USER_ID, out=False, forwarded=True)))

    def test_someone_elses_marked_file_is_kept(self):
        self.assertFalse(_twin(_message(sender=USER_ID, out=False)))

    def test_unmarked_and_meta_captions_are_not_twins(self):
        self.assertFalse(_twin(_message("**Title**")))
        self.assertFalse(_twin(_message(f"{BOT_META_INFO_PREFIX}Log")))
        self.assertFalse(_twin(_message("")))

    def test_a_marked_text_message_is_not_a_twin(self):
        self.assertFalse(_twin(_message(media=False)))

    def test_without_our_id_only_sender_less_outgoing_messages_count(self):
        self.assertFalse(history_util.is_twin_file(_message(), self_id=None))
        self.assertTrue(history_util.is_twin_file(_message(sender=None), self_id=None))


if __name__ == "__main__":
    unittest.main()
