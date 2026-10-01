"""`callback_util.hold_bare_answers`, on Telethon's real CallbackQuery.Event."""

import asyncio
import unittest

from telethon import errors, events
from telethon.tl import functions, types

from uniborg import callback_util


class _Client:
    """Records the answers and edits a real CallbackQuery.Event sends."""

    def __init__(self, *, edit_error=None, answer_error=None):
        self.loop = asyncio.get_running_loop()
        self.sent = []
        self.edit_error = edit_error
        self.answer_error = answer_error

    async def __call__(self, request):
        assert isinstance(request, functions.messages.SetBotCallbackAnswerRequest)
        await asyncio.sleep(0)
        if self.answer_error:
            raise self.answer_error
        self.sent.append(("answer", request.message, request.alert))

    async def edit_message(self, *args, **kwargs):
        await asyncio.sleep(0.01)
        if self.edit_error:
            raise self.edit_error
        self.sent.append(("edit",))


def _press(client):
    query = types.UpdateBotCallbackQuery(
        query_id=7,
        user_id=1,
        peer=types.PeerUser(1),
        msg_id=5,
        chat_instance=0,
        data=b"x",
    )
    event = events.CallbackQuery.Event(query, query.peer, query.msg_id)
    event._client = client
    event._input_chat = types.InputPeerUser(1, 0)
    return event


async def _settle():
    """Lets the answer tasks that Telethon schedules run."""
    for _ in range(5):
        await asyncio.sleep(0.01)


class HoldBareAnswersTests(unittest.IsolatedAsyncioTestCase):
    async def press(self, handler, **client_kwargs):
        client = _Client(**client_kwargs)
        await handler(_press(client))
        await _settle()
        return client.sent

    async def test_without_the_wrapper_an_edit_swallows_the_toast(self):
        async def handler(event):
            await event.edit(buttons=None)
            await event.answer("Saved.")

        self.assertEqual(
            await self.press(handler), [("answer", None, False), ("edit",)]
        )

    async def test_a_toast_after_an_edit_shows(self):
        @callback_util.hold_bare_answers
        async def handler(event):
            await event.edit(buttons=None)
            await event.answer("Saved.")

        self.assertEqual(
            await self.press(handler), [("edit",), ("answer", "Saved.", False)]
        )

    async def test_an_alert_after_an_edit_shows(self):
        @callback_util.hold_bare_answers
        async def handler(event):
            await event.edit(buttons=None)
            await event.answer("Not allowed.", alert=True)

        self.assertEqual(
            await self.press(handler), [("edit",), ("answer", "Not allowed.", True)]
        )

    async def test_a_toast_after_a_failed_edit_shows(self):
        @callback_util.hold_bare_answers
        async def handler(event):
            try:
                await event.edit(buttons=None)
            except errors.MessageNotModifiedError:
                pass
            await event.answer("Already on.")

        sent = await self.press(
            handler, edit_error=errors.MessageNotModifiedError(request=None)
        )
        self.assertEqual(sent, [("answer", "Already on.", False)])

    async def test_an_edit_alone_is_answered_once_without_text(self):
        @callback_util.hold_bare_answers
        async def handler(event):
            await event.edit(buttons=None)
            await event.answer()

        self.assertEqual(
            await self.press(handler), [("edit",), ("answer", None, False)]
        )

    async def test_a_handler_that_never_answers_still_stops_the_spinner(self):
        @callback_util.hold_bare_answers
        async def handler(event):
            return "ignored"

        self.assertEqual(await self.press(handler), [("answer", None, False)])

    async def test_a_raising_handler_is_answered_and_still_raises(self):
        @callback_util.hold_bare_answers
        async def handler(event):
            await event.edit(buttons=None)
            raise RuntimeError("boom")

        client = _Client()
        with self.assertRaises(RuntimeError):
            await handler(_press(client))
        await _settle()
        self.assertEqual(client.sent, [("edit",), ("answer", None, False)])

    async def test_a_slow_handler_is_answered_by_the_fallback(self):
        reached = asyncio.Event()

        @callback_util.hold_bare_answers(fallback_seconds=0.05)
        async def handler(event):
            await event.edit(buttons=None)
            await asyncio.sleep(0.2)
            reached.set()
            await event.answer("Too late.")

        sent = await self.press(handler)
        self.assertTrue(reached.is_set())
        self.assertEqual(sent, [("edit",), ("answer", None, False)])

    async def test_a_toast_before_the_edit_is_the_only_answer(self):
        @callback_util.hold_bare_answers
        async def handler(event):
            await event.answer("Working…")
            await event.edit(buttons=None)

        self.assertEqual(
            await self.press(handler), [("answer", "Working…", False), ("edit",)]
        )

    async def test_an_answer_that_fails_is_logged_not_raised(self):
        @callback_util.hold_bare_answers
        async def handler(event):
            await event.edit(buttons=None)

        with self.assertLogs(callback_util.__name__, level="WARNING"):
            sent = await self.press(
                handler, answer_error=errors.QueryIdInvalidError(request=None)
            )
        self.assertEqual(sent, [("edit",)])

    async def test_the_wrapper_keeps_the_handler_module(self):
        async def handler(event):
            pass

        handler.__module__ = "_UniborgPlugins.test.fake_plugin"

        wrapped = callback_util.hold_bare_answers(handler)

        self.assertEqual(wrapped.__module__, "_UniborgPlugins.test.fake_plugin")
        self.assertIs(wrapped.__wrapped__, handler)


if __name__ == "__main__":
    unittest.main()
