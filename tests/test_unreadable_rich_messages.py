import asyncio
import builtins
import importlib
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from telethon.tl.types import (
    MessageMediaDocument,
    MessageMediaPhoto,
    MessageMediaUnsupported,
    MessageMediaWebPage,
    WebPageEmpty,
)

from uniborg.constants import BOT_META_INFO_PREFIX


class _FakeLoop:
    def create_task(self, coro):
        coro.close()


class _FakeBorg:
    loop = _FakeLoop()

    def on(self, *args, **kwargs):
        def decorator(func):
            return func

        return decorator


def _import_plugins():
    #: stt registers handlers at import, so it needs `.on` as well as `.loop`.
    #: Restore whatever borg an earlier test module installed.
    previous = getattr(builtins, "borg", None)
    builtins.borg = _FakeBorg()
    try:

        async def _import():
            return (
                importlib.import_module("llm_chat_plugins.llm_chat"),
                importlib.import_module("stt_plugins.stt"),
            )

        return asyncio.run(_import())
    finally:
        if previous is not None:
            builtins.borg = previous


llm_chat, stt = _import_plugins()


def _message(*, text="", media=None, message_id=7, **extra):
    return SimpleNamespace(
        id=message_id,
        text=text,
        media=media,
        reply_to_msg_id=None,
        reactions=None,
        forward=None,
        **extra,
    )


def _rich_224():
    """A rich message as layer 224 (Telethon 1.43) receives it."""
    return _message(media=MessageMediaUnsupported())


def _rich_229():
    """A rich message as layer 229 (Telethon 1.45) receives it."""
    return _message(rich_message=object())


def _process(message, *, role="user", export_mode=False, metadata_prefix=""):
    return asyncio.run(
        llm_chat._process_message_content(
            message,
            role,
            Path("/nonexistent"),
            {},
            set(),
            "key",
            "gemini/gemini-test",
            1,
            metadata_prefix=metadata_prefix,
            is_private=True,
            export_mode=export_mode,
        )
    )


class UnreadableRichPredicateTests(unittest.TestCase):
    def test_layer_224_downgrade_is_unreadable(self):
        self.assertTrue(llm_chat._is_unreadable_rich_message(_rich_224()))

    def test_layer_229_rich_message_is_unreadable(self):
        self.assertTrue(llm_chat._is_unreadable_rich_message(_rich_229()))

    def test_text_makes_it_readable(self):
        #: A caption on unsupported media is still something we can read.
        message = _message(text="caption", media=MessageMediaUnsupported())
        self.assertFalse(llm_chat._is_unreadable_rich_message(message))

    def test_ordinary_media_is_not_rich(self):
        message = _message(media=MessageMediaPhoto())
        self.assertFalse(llm_chat._is_unreadable_rich_message(message))

    def test_empty_message_is_not_rich(self):
        self.assertFalse(llm_chat._is_unreadable_rich_message(_message()))
        self.assertFalse(llm_chat._is_unreadable_rich_message(None))


class UnreadableRichHistoryTests(unittest.TestCase):
    def test_placeholder_replaces_media_handling(self):
        with patch.object(llm_chat, "_process_media", new=AsyncMock()) as media:
            result = _process(_rich_224())

        media.assert_not_awaited()
        self.assertEqual(
            result.text_parts, [llm_chat.UNREADABLE_RICH_MESSAGE_PLACEHOLDER]
        )
        self.assertEqual(result.media_parts, [])

    def test_layer_229_form_gets_the_placeholder(self):
        result = _process(_rich_229())
        self.assertEqual(
            result.text_parts, [llm_chat.UNREADABLE_RICH_MESSAGE_PLACEHOLDER]
        )
        self.assertEqual(result.media_parts, [])

    def test_placeholder_keeps_metadata_prefix(self):
        with patch.object(llm_chat, "_process_media", new=AsyncMock()):
            result = _process(_rich_224(), metadata_prefix="[meta]")

        self.assertEqual(
            result.text_parts,
            ["[meta]\n" + llm_chat.UNREADABLE_RICH_MESSAGE_PLACEHOLDER],
        )

    def test_export_mode_makes_no_file_reference(self):
        with patch.object(
            llm_chat, "_create_export_file_reference", new=AsyncMock()
        ) as file_ref:
            result = _process(_rich_224(), export_mode=True)

        file_ref.assert_not_awaited()
        self.assertEqual(
            result.text_parts, [llm_chat.UNREADABLE_RICH_MESSAGE_PLACEHOLDER]
        )
        self.assertEqual(result.media_parts, [])

    def test_ordinary_media_is_still_tagged_and_processed(self):
        media_result = llm_chat.ProcessMediaResult(media_part=None, warnings=[])
        with patch.object(
            llm_chat, "_process_media", new=AsyncMock(return_value=media_result)
        ) as media:
            result = _process(_message(media=MessageMediaPhoto()))

        media.assert_awaited_once()
        self.assertEqual(result.text_parts, ["[Media-ID: 7]"])

    def test_meta_info_messages_are_still_dropped(self):
        message = _message(text=f"{BOT_META_INFO_PREFIX}status")
        result = _process(message, role="assistant")
        self.assertEqual(result.text_parts, [])

    def test_reply_to_unreadable_message_quotes_the_placeholder(self):
        parent = _message(
            media=MessageMediaUnsupported(),
            message_id=3,
            date=None,
            get_sender=AsyncMock(return_value=SimpleNamespace(username="alice")),
        )
        message = _message(text="what did you mean?")
        message.reply_to_msg_id = 3

        quote = asyncio.run(llm_chat._build_reply_quote(message, {3: parent}))

        self.assertEqual(
            quote,
            f"[Replying to @alice]:\n> {llm_chat.UNREADABLE_RICH_MESSAGE_PLACEHOLDER}",
        )

    def _history(self, metadata_mode):
        prefs = SimpleNamespace(
            metadata_mode=metadata_mode, group_metadata_mode=metadata_mode
        )
        rich = _rich_224()
        plain = _message(text="hello", message_id=8)
        rich._role = plain._role = "user"
        event = SimpleNamespace(sender_id=1, is_private=True)
        with patch.object(
            llm_chat.user_manager, "get_prefs", return_value=prefs
        ), patch.object(llm_chat, "_process_media", new=AsyncMock()):
            history, _ = asyncio.run(
                llm_chat._process_turns_to_history(
                    event,
                    [rich, plain],
                    Path("/nonexistent"),
                    {},
                    "key",
                    "gemini/gemini-test",
                    is_private=True,
                    include_system_prompt_p=False,
                )
            )
        return history

    def test_separate_turns_keep_the_placeholder_turn(self):
        history = self._history("only_forwarded")
        self.assertEqual(
            history,
            [
                {
                    "role": "user",
                    "content": llm_chat.UNREADABLE_RICH_MESSAGE_PLACEHOLDER,
                },
                {"role": "user", "content": "hello"},
            ],
        )

    def test_merged_turns_keep_the_placeholder(self):
        history = self._history("no_metadata")
        self.assertEqual(
            history,
            [
                {
                    "role": "user",
                    "content": llm_chat.UNREADABLE_RICH_MESSAGE_PLACEHOLDER + "\nhello",
                }
            ],
        )


class UnreadableRichTriggerTests(unittest.TestCase):
    def _event(self, message):
        return SimpleNamespace(
            sender_id=1,
            chat_id=1,
            message=message,
            text=message.text,
            media=message.media,
            forward=None,
            is_private=True,
            out=False,
        )

    def test_private_rich_messages_reach_the_handler(self):
        for message in (_rich_224(), _rich_229()):
            with self.subTest(message=message):
                self.assertTrue(
                    asyncio.run(llm_chat.is_valid_chat_message(self._event(message)))
                )

    def test_empty_messages_are_still_rejected(self):
        self.assertFalse(
            asyncio.run(llm_chat.is_valid_chat_message(self._event(_message())))
        )

    def _is_valid_in_group(self, *, reply_sender_id):
        event = self._event(_rich_224())
        event.is_private = False
        event.is_reply = reply_sender_id is not None
        event.get_reply_message = AsyncMock(
            return_value=SimpleNamespace(sender_id=reply_sender_id)
        )
        prefs = SimpleNamespace(group_activation_mode="mention_and_reply")
        with patch.object(llm_chat, "BOT_USERNAME", "@testbot"), patch.object(
            llm_chat.user_manager, "get_prefs", return_value=prefs
        ), patch.object(builtins, "borg", SimpleNamespace(me=SimpleNamespace(id=42))):
            return asyncio.run(llm_chat.is_valid_chat_message(event))

    def test_group_rich_reply_to_the_bot_reaches_the_handler(self):
        self.assertTrue(self._is_valid_in_group(reply_sender_id=42))

    def test_group_rich_message_not_addressed_to_the_bot_is_ignored(self):
        #: Without text there is no mention, so only a reply to the bot counts.
        for reply_sender_id in (None, 7):
            with self.subTest(reply_sender_id=reply_sender_id):
                self.assertFalse(
                    self._is_valid_in_group(reply_sender_id=reply_sender_id)
                )

    def test_handler_asks_for_plain_text_and_skips_the_llm(self):
        event = self._event(_rich_224())
        live_mode = Mock(return_value=False)
        with patch.object(llm_chat, "cleanup_completed_tasks"), patch.object(
            llm_chat.llm_db, "is_awaiting_key", return_value=False
        ), patch.object(
            llm_chat, "send_info_message", new=AsyncMock()
        ) as send_info, patch.object(
            llm_chat.gemini_live_util.live_session_manager,
            "is_live_mode_active",
            new=live_mode,
        ):
            asyncio.run(llm_chat.chat_handler(event))

        send_info.assert_awaited_once_with(
            event, llm_chat.UNREADABLE_RICH_MESSAGE_NOTICE
        )
        live_mode.assert_not_called()


class SttMediaFilterTests(unittest.TestCase):
    def _event(self, media, *, sender=True):
        return SimpleNamespace(media=media, sender=(object() if sender else None))

    def test_transcribable_media_passes(self):
        for media in (MessageMediaDocument(), MessageMediaPhoto()):
            with self.subTest(media=type(media).__name__):
                self.assertTrue(stt.is_transcribable_media_event(self._event(media)))

    def test_rich_messages_and_link_previews_are_ignored(self):
        for media in (
            MessageMediaUnsupported(),
            MessageMediaWebPage(webpage=WebPageEmpty(id=1)),
        ):
            with self.subTest(media=type(media).__name__):
                self.assertFalse(stt.is_transcribable_media_event(self._event(media)))

    def test_no_media_or_no_sender_is_ignored(self):
        self.assertFalse(stt.is_transcribable_media_event(self._event(None)))
        self.assertFalse(
            stt.is_transcribable_media_event(
                self._event(MessageMediaDocument(), sender=False)
            )
        )


if __name__ == "__main__":
    unittest.main()
