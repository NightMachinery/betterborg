import asyncio
from types import SimpleNamespace
import unittest

from telethon import errors, functions, types

from uniborg import telethon_safety, tg_raw

GUEST_TYPES = hasattr(functions.messages, "SetBotGuestChatResultRequest")


class _FakeSender:
    def __init__(self, outcome):
        self.outcome = outcome
        self.sent = []

    def send(self, request, ordered=False):
        self.sent.append(request)
        future = asyncio.get_running_loop().create_future()
        if isinstance(self.outcome, BaseException):
            future.set_exception(self.outcome)
        else:
            future.set_result(self.outcome)
        self.last_future = future
        return future


class _FakeSession:
    def __init__(self, dc_id=2):
        self.dc_id = dc_id
        self.processed = []

    def process_entities(self, result):
        self.processed.append(result)


class _FakeClient:
    def __init__(self, *, outcome=None, dc_id=2, edit_error=None):
        self._sender = _FakeSender(outcome)
        self.session = _FakeSession(dc_id)
        self.calls = []
        self.borrowed = []
        self.returned = []
        self.edit_error = edit_error
        self.parsed = []

    async def __call__(self, request):
        self.calls.append(("home", request))
        if self.edit_error:
            raise self.edit_error
        return True

    async def _call(self, sender, request):
        self.calls.append((sender, request))
        if self.edit_error:
            raise self.edit_error
        return True

    async def _borrow_exported_sender(self, dc_id):
        sender = f"sender-{dc_id}"
        self.borrowed.append(sender)
        return sender

    async def _return_exported_sender(self, sender):
        self.returned.append(sender)

    async def _parse_message_text(self, text, parse_mode):
        self.parsed.append((text, parse_mode))
        return text.strip("*"), [types.MessageEntityBold(0, len(text.strip("*")))]

    def build_reply_markup(self, buttons):
        return ("markup", buttons)


def _inline_id(dc_id=2):
    return types.InputBotInlineMessageID(dc_id=dc_id, id=7, access_hash=8)


@unittest.skipUnless(GUEST_TYPES, "needs Telethon 1.45 guest-mode types")
class AnswerGuestTests(unittest.TestCase):
    def test_sends_once_through_the_sender_and_marks_it_at_most_once(self):
        client = _FakeClient(outcome=_inline_id())

        result = asyncio.run(
            tg_raw.answer_guest(client, query_id=5, title="Shell", text="⏳")
        )

        self.assertEqual(result, _inline_id())
        (request,) = client._sender.sent
        self.assertIsInstance(request, functions.messages.SetBotGuestChatResultRequest)
        self.assertEqual(request.query_id, 5)
        self.assertEqual(request.result.title, "Shell")
        self.assertEqual(request.result.type, "article")
        self.assertIsInstance(
            request.result.send_message, types.InputBotInlineMessageText
        )
        self.assertIn(client._sender.last_future, telethon_safety._AT_MOST_ONCE_FUTURES)
        self.assertEqual(client.session.processed, [_inline_id()])
        self.assertEqual(client.calls, [])

    def test_a_server_error_is_not_retried(self):
        client = _FakeClient(outcome=errors.ServerError(request=None, message="x"))

        with self.assertRaises(errors.ServerError):
            asyncio.run(tg_raw.answer_guest(client, query_id=5, title="T", text="a"))

        self.assertEqual(len(client._sender.sent), 1)

    def test_markdown_becomes_a_rich_answer(self):
        client = _FakeClient(outcome=_inline_id())

        asyncio.run(
            tg_raw.answer_guest(
                client, query_id=5, title="T", markdown="# Hi", buttons=[["b"]]
            )
        )

        message = client._sender.sent[0].result.send_message
        self.assertIsInstance(message, types.InputBotInlineMessageRichMessage)
        self.assertEqual(message.rich_message.markdown, "# Hi")
        self.assertEqual(message.reply_markup, ("markup", [["b"]]))

    def test_rejects_a_missing_title_and_ambiguous_content(self):
        client = _FakeClient(outcome=_inline_id())
        for kwargs in (
            dict(title="", text="a"),
            dict(title="T"),
            dict(title="T", text="a", markdown="b"),
        ):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(ValueError):
                    asyncio.run(tg_raw.answer_guest(client, query_id=5, **kwargs))
        self.assertEqual(client._sender.sent, [])


@unittest.skipUnless(GUEST_TYPES, "needs Telethon 1.45 rich inline edits")
class InlineEditorTests(unittest.TestCase):
    def test_home_dc_edits_go_through_the_client(self):
        client = _FakeClient(dc_id=2)

        async def run():
            async with tg_raw.InlineEditor(client, _inline_id(2)) as editor:
                return await editor.edit(text="hi")

        self.assertTrue(asyncio.run(run()))
        ((route, request),) = client.calls
        self.assertEqual(route, "home")
        self.assertIsInstance(request, functions.messages.EditInlineBotMessageRequest)
        self.assertEqual(request.message, "hi")
        self.assertIsNone(request.rich_message)
        self.assertEqual(client.borrowed, [])

    def test_other_dc_borrows_once_and_returns_the_sender_on_error(self):
        client = _FakeClient(dc_id=2, edit_error=RuntimeError("boom"))

        async def run():
            async with tg_raw.InlineEditor(client, _inline_id(4)) as editor:
                await editor.edit(text="hi")

        with self.assertRaises(RuntimeError):
            asyncio.run(run())

        self.assertEqual(client.borrowed, ["sender-4"])
        self.assertEqual(client.returned, ["sender-4"])
        self.assertEqual(client.calls[0][0], "sender-4")

    def test_not_modified_returns_false(self):
        client = _FakeClient(edit_error=errors.MessageNotModifiedError(request=None))

        async def run():
            async with tg_raw.InlineEditor(client, _inline_id()) as editor:
                return await editor.edit(text="same")

        self.assertFalse(asyncio.run(run()))

    def test_markdown_parse_mode_and_media(self):
        client = _FakeClient()
        media = types.InputMediaDocument(
            id=types.InputDocument(id=1, access_hash=2, file_reference=b"")
        )

        async def run():
            async with tg_raw.InlineEditor(client, _inline_id()) as editor:
                await editor.edit(markdown="## Done")
                await editor.edit(text="**b**", parse_mode="md", media=media)

        asyncio.run(run())

        rich, parsed = (request for _route, request in client.calls)
        self.assertEqual(rich.rich_message.markdown, "## Done")
        self.assertIsNone(rich.message)
        self.assertEqual(client.parsed, [("**b**", "md")])
        self.assertEqual(parsed.message, "b")
        self.assertIsInstance(parsed.entities[0], types.MessageEntityBold)
        self.assertIs(parsed.media, media)

    def test_rejects_text_and_markdown_together(self):
        client = _FakeClient()

        async def run():
            async with tg_raw.InlineEditor(client, _inline_id()) as editor:
                await editor.edit(text="a", markdown="b")

        with self.assertRaises(ValueError):
            asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
