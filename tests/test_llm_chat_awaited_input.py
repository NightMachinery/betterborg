"""Awaited input (the flows that wait for the user's next message) in topics.

Terms used below:

- A *flow* is an entry in `AWAITING_INPUT_FROM_USERS`: a prompt such as
  "send a custom model ID below" waiting for the user's next message.
- A *private topic* is a topic in a bot's private chat (threaded mode). The
  shapes are the ones a canary bot observed live (Telethon 1.45, layer 229;
  layer 224 carries the same `MessageReplyHeader` fields): every message in a
  topic has `forum_topic=True` and the topic id in `reply_to_top_id`, and a
  message typed in "All" opens a new topic of its own.
- *Outside topics* means a private chat without threaded mode: no reply
  header at all.
"""

import asyncio
import builtins
import importlib
import unittest
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from telethon.tl.types import Message, MessageReplyHeader, PeerChannel, PeerUser

from uniborg import llm_db, tg_compat
from uniborg.constants import BOT_META_INFO_PREFIX


class _FakeLoop:
    def create_task(self, coro):
        coro.close()


class _FakeBorg:
    loop = _FakeLoop()


def _import_plugin():
    previous = getattr(builtins, "borg", None)
    builtins.borg = _FakeBorg()
    try:

        async def _import():
            return importlib.import_module("llm_chat_plugins.llm_chat")

        return asyncio.run(_import())
    finally:
        if previous is not None:
            builtins.borg = previous


plugin = _import_plugin()

USER_ID = 999000781
CHANNEL_ID = 999000783
GROUP_CHAT_ID = -1000000000000 - CHANNEL_ID

#: Two private topics: each root in the bot's box, each id from the user's.
ROOT_ID, TOPIC_ID = 322, 1241380
OTHER_ROOT_ID, OTHER_TOPIC_ID = 326, 1241384


class _Event:
    """Forwards to its message the way Telethon's NewMessage event does."""

    def __init__(self, message, **overrides):
        self.message = message
        self.sender_id = USER_ID
        self.chat_id = USER_ID
        self.is_private = True
        self.text = message.message
        self.raw_text = message.message
        self.grouped_id = None
        self.pattern_match = SimpleNamespace(group=lambda _index: None)
        self.reply = AsyncMock()
        self.__dict__.update(overrides)

    def __getattr__(self, name):
        return getattr(self.message, name)


def _in_topic(text, *, msg_id=500, top_id=TOPIC_ID, parent=ROOT_ID):
    """A private-chat message in topic TOP_ID; PARENT=the root makes it plain."""
    return _Event(
        Message(
            id=msg_id,
            peer_id=PeerUser(USER_ID),
            message=text,
            reply_to=MessageReplyHeader(
                forum_topic=True, reply_to_msg_id=parent, reply_to_top_id=top_id
            ),
        )
    )


def _typed_in_all(text, *, msg_id=502):
    """What a message typed in "All" becomes: the first one of a new topic."""
    return _in_topic(text, msg_id=msg_id, top_id=OTHER_TOPIC_ID, parent=OTHER_ROOT_ID)


def _outside_topics(text, *, msg_id=500):
    return _Event(Message(id=msg_id, peer_id=PeerUser(USER_ID), message=text))


def _in_forum_group(text, *, msg_id=500):
    return _Event(
        Message(
            id=msg_id,
            peer_id=PeerChannel(CHANNEL_ID),
            message=text,
            reply_to=MessageReplyHeader(
                forum_topic=True, reply_to_msg_id=40, reply_to_top_id=40
            ),
        ),
        chat_id=GROUP_CHAT_ID,
        is_private=False,
    )


class _IsolatedStateTest(unittest.TestCase):
    """Runs each test against empty pending-input and pending-key state."""

    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.pending = {}
        self.stack.enter_context(
            patch.object(plugin, "AWAITING_INPUT_FROM_USERS", self.pending)
        )
        self.stack.enter_context(
            patch.dict(llm_db.AWAITING_KEY_FROM_USERS, {}, clear=True)
        )


class StartInputFlowTests(_IsolatedStateTest):
    def test_outside_topics_the_flow_is_stored_as_before(self):
        flow = {"type": "model"}
        plugin.start_input_flow(_outside_topics("/setmodel"), flow)
        self.assertEqual(self.pending, {USER_ID: {"type": "model"}})
        self.assertIs(self.pending[USER_ID], flow)

    def test_in_a_private_topic_the_flow_records_the_topic(self):
        flow = {"type": "model"}
        plugin.start_input_flow(_in_topic("/setmodel"), flow)
        self.assertEqual(
            self.pending[USER_ID],
            {
                "type": "model",
                plugin.INPUT_TOPIC_KEY: plugin.InputTopic(
                    chat_id=USER_ID, topic_id=TOPIC_ID
                ),
            },
        )
        self.assertEqual(flow, {"type": "model"})

    def test_a_forum_group_topic_is_not_recorded(self):
        plugin.start_input_flow(
            _in_forum_group("/setmodelhere"), {"type": "chatmodel", "chat_id": 1}
        )
        self.assertEqual(self.pending[USER_ID], {"type": "chatmodel", "chat_id": 1})

    def test_the_store_can_be_injected(self):
        store = {}
        plugin.start_input_flow(
            _in_topic("/setmodel"), {"type": "model"}, pending_inputs=store
        )
        self.assertIn(USER_ID, store)
        self.assertEqual(self.pending, {})


class PendingInputFlowTests(_IsolatedStateTest):
    def test_a_flow_from_outside_topics_is_answered_from_anywhere(self):
        plugin.start_input_flow(_outside_topics("/setmodel"), {"type": "model"})
        for event in (
            _outside_topics("x"),
            _in_topic("x"),
            _typed_in_all("x"),
            _in_forum_group("x"),
        ):
            self.assertIs(plugin.pending_input_flow(event), self.pending[USER_ID])

    def test_a_topic_flow_is_answered_in_its_topic(self):
        plugin.start_input_flow(_in_topic("/setmodel", msg_id=400), {"type": "model"})
        plain = _in_topic("provider/custom", msg_id=402)
        explicit_reply = _in_topic("provider/custom", msg_id=403, parent=401)
        for event in (plain, explicit_reply):
            self.assertIs(plugin.pending_input_flow(event), self.pending[USER_ID])
            self.assertTrue(plugin._answers_pending_input_flow(event))

    def test_a_topic_flow_is_not_answered_from_elsewhere(self):
        plugin.start_input_flow(_in_topic("/setmodel"), {"type": "model"})
        for event in (
            _typed_in_all("Hi"),
            _outside_topics("Hi"),
            _in_forum_group("Hi"),
        ):
            self.assertIsNone(plugin.pending_input_flow(event))
            self.assertFalse(plugin._answers_pending_input_flow(event))
        self.assertIn(USER_ID, self.pending)

    def test_the_store_can_be_injected(self):
        store = {}
        plugin.start_input_flow(
            _in_topic("/setmodel"), {"type": "model"}, pending_inputs=store
        )
        self.assertIsNone(plugin.pending_input_flow(_in_topic("x")))
        self.assertIs(
            plugin.pending_input_flow(_in_topic("x"), pending_inputs=store),
            store[USER_ID],
        )


class FlowStartingHandlerTests(_IsolatedStateTest):
    """Each handler that asks for the next message records where it asked."""

    def setUp(self):
        super().setUp()
        enter = self.stack.enter_context
        config = object()
        enter(patch.object(plugin.llm_chat_config, "load_config", return_value=config))
        enter(
            patch.object(
                plugin.llm_chat_config,
                "can_use_codex",
                new=AsyncMock(return_value=False),
            )
        )
        enter(patch.object(plugin.util, "isAdmin", new=AsyncMock(return_value=False)))
        enter(
            patch.object(
                plugin.util, "is_group_admin", new=AsyncMock(return_value=False)
            )
        )
        enter(
            patch.object(
                plugin,
                "_build_model_menu",
                return_value=SimpleNamespace(options={}, current_value=None),
            )
        )
        enter(patch.object(plugin.bot_util, "present_options", new=AsyncMock()))
        enter(patch.object(plugin.chat_manager, "get_model", return_value=None))
        enter(
            patch.object(
                plugin.user_manager,
                "get_prefs",
                return_value=SimpleNamespace(
                    system_prompt="Be brief.", enabled_tools=[]
                ),
            )
        )
        enter(patch.object(plugin, "send_info_message", new=AsyncMock()))
        enter(patch.object(plugin, "IS_BOT", False))

    def run_handler(self, handler, event):
        asyncio.run(handler(event))
        return self.pending.get(USER_ID)

    def test_set_model(self):
        flow = self.run_handler(plugin.set_model_handler, _in_topic("/setmodel"))
        self.assertEqual(flow["type"], "model")
        self.assertEqual(flow[plugin.INPUT_TOPIC_KEY].topic_id, TOPIC_ID)

    def test_set_system_prompt(self):
        flow = self.run_handler(
            plugin.set_system_prompt_handler, _in_topic("/setsystemprompt")
        )
        self.assertEqual(flow["type"], "system_prompt")
        self.assertEqual(flow[plugin.INPUT_TOPIC_KEY].topic_id, TOPIC_ID)

    def test_set_model_here(self):
        flow = self.run_handler(
            plugin.set_model_here_handler, _in_topic("/setmodelhere")
        )
        self.assertEqual(flow["type"], "chatmodel")
        self.assertEqual(flow["chat_id"], USER_ID)
        self.assertEqual(flow[plugin.INPUT_TOPIC_KEY].topic_id, TOPIC_ID)

    def test_outside_topics_each_handler_stores_the_flow_as_before(self):
        cases = (
            (plugin.set_model_handler, {"type": "model"}),
            (plugin.set_system_prompt_handler, {"type": "system_prompt"}),
            (
                plugin.set_model_here_handler,
                {"type": "chatmodel", "chat_id": USER_ID},
            ),
            (
                plugin.tools_handler,
                {"type": "tool_selection", "keys": plugin.AVAILABLE_TOOLS},
            ),
        )
        for handler, expected in cases:
            with self.subTest(handler=handler.__name__):
                self.pending.clear()
                self.assertEqual(
                    self.run_handler(handler, _outside_topics("/command")), expected
                )


MENU_ID = 700


def _press(data, *, message_id=MENU_ID, chat_id=USER_ID, is_private=True):
    """A press on a button of message MESSAGE_ID; ORDER logs answer and edit."""
    order = Mock()
    order.attach_mock(AsyncMock(), "answer")
    order.attach_mock(AsyncMock(), "edit")
    return SimpleNamespace(
        data=data.encode(),
        sender_id=USER_ID,
        chat_id=chat_id,
        message_id=message_id,
        is_private=is_private,
        answer=order.answer,
        edit=order.edit,
        order=order,
    )


class ModelMenuCancelTests(_IsolatedStateTest):
    """A bot's model menus carry a Cancel row that drops the flow they armed."""

    def setUp(self):
        super().setUp()
        enter = self.stack.enter_context
        self.menu = plugin.ModelMenu(
            options={"model/a": "Model A", "model/b": "Model B"},
            current_value="model/a",
            think_state=None,
        )
        enter(patch.object(plugin, "IS_BOT", True))
        enter(patch.object(plugin.llm_chat_config, "load_config", return_value=None))
        enter(
            patch.object(
                plugin.llm_chat_config,
                "can_use_codex",
                new=AsyncMock(return_value=False),
            )
        )
        self.admin = enter(
            patch.object(plugin.util, "isAdmin", new=AsyncMock(return_value=False))
        )
        enter(
            patch.object(
                plugin.util, "is_group_admin", new=AsyncMock(return_value=False)
            )
        )
        enter(patch.object(plugin, "_build_model_menu", return_value=self.menu))
        enter(patch.object(plugin.chat_manager, "get_model", return_value=None))
        enter(
            patch.object(
                plugin.user_manager,
                "get_prefs",
                return_value=SimpleNamespace(model="model/a"),
            )
        )
        enter(patch.object(plugin, "send_info_message", new=AsyncMock()))

    def open_menu(self, handler=None, event=None):
        event = event or _in_topic("/setmodel")
        event.reply = AsyncMock(return_value=SimpleNamespace(id=MENU_ID))
        asyncio.run((handler or plugin.set_model_handler)(event))
        return event

    def test_the_menu_is_one_message_with_a_cancel_row(self):
        event = self.open_menu()

        ((text,), kwargs) = event.reply.await_args
        self.assertTrue(text.startswith(BOT_META_INFO_PREFIX))
        self.assertIn("send a custom model ID", text)
        (cancel,) = kwargs["buttons"][-1]
        self.assertEqual(tg_compat.button_data(cancel), b"mm:cancel:personal")
        flow = self.pending[USER_ID]
        self.assertEqual(flow["type"], "model")
        self.assertEqual(
            flow[plugin.INPUT_MENU_KEY], plugin.InputMenu(USER_ID, MENU_ID)
        )
        self.assertEqual(flow[plugin.INPUT_TOPIC_KEY].topic_id, TOPIC_ID)

    def test_the_chat_menu_cancel_names_its_scope(self):
        event = self.open_menu(plugin.set_model_here_handler, _in_topic("/smh"))

        (cancel,) = event.reply.await_args.kwargs["buttons"][-1]
        self.assertEqual(tg_compat.button_data(cancel), b"mm:cancel:chat")
        self.assertEqual(self.pending[USER_ID]["chat_id"], USER_ID)

    def test_cancel_drops_the_flow_then_closes_the_menu(self):
        self.open_menu()
        press = _press("mm:cancel:personal")

        asyncio.run(plugin.callback_handler(press))

        self.assertNotIn(USER_ID, self.pending)
        self.assertEqual(
            [call[0] for call in press.order.mock_calls], ["answer", "edit"]
        )
        ((text,), kwargs) = press.edit.await_args
        self.assertTrue(text.startswith(BOT_META_INFO_PREFIX))
        self.assertIn("Cancelled. Current model: `model/a`.", text)
        self.assertIsNone(kwargs["buttons"])

    def test_cancel_on_an_older_menu_keeps_the_newer_flow(self):
        self.open_menu()
        press = _press("mm:cancel:personal", message_id=MENU_ID - 1)

        asyncio.run(plugin.callback_handler(press))

        self.assertIn(USER_ID, self.pending)
        press.edit.assert_awaited_once()

    def test_cancel_with_no_flow_still_closes_the_menu(self):
        press = _press("mm:cancel:chat")

        asyncio.run(plugin.callback_handler(press))

        self.assertIn("not set", press.edit.await_args.args[0])

    def test_a_group_menu_needs_an_admin_and_a_bogus_scope_is_refused(self):
        group_menu = {"type": "chatmodel", "chat_id": GROUP_CHAT_ID}
        group_menu[plugin.INPUT_MENU_KEY] = plugin.InputMenu(GROUP_CHAT_ID, MENU_ID)
        self.pending[USER_ID] = group_menu
        for data in ("mm:cancel:chat", "mm:cancel:bogus"):
            with self.subTest(data=data):
                press = _press(data, chat_id=GROUP_CHAT_ID, is_private=False)
                asyncio.run(plugin.callback_handler(press))
                self.assertTrue(press.answer.await_args.kwargs["show_alert"])
                press.edit.assert_not_awaited()
        self.assertIn(USER_ID, self.pending)

        self.admin.return_value = True
        asyncio.run(
            plugin.callback_handler(
                _press("mm:cancel:chat", chat_id=GROUP_CHAT_ID, is_private=False)
            )
        )
        self.assertNotIn(USER_ID, self.pending)

    def test_a_typed_cancel_closes_the_menu_too(self):
        self.open_menu()
        typed = _in_topic("cancel", msg_id=501)
        typed.client = SimpleNamespace(edit_message=AsyncMock())

        asyncio.run(plugin.generic_input_handler(typed))

        self.assertNotIn(USER_ID, self.pending)
        args, kwargs = typed.client.edit_message.await_args
        self.assertEqual(args[:2], (USER_ID, MENU_ID))
        self.assertIn("Cancelled.", args[2])
        self.assertIsNone(kwargs["buttons"])

    def test_picking_a_model_keeps_the_cancel_row(self):
        self.stack.enter_context(
            patch.object(
                plugin,
                "_model_choices_for_access",
                return_value={"model/b": "Model B"},
            )
        )
        self.stack.enter_context(
            patch.object(
                plugin, "_can_user_access_model", new=AsyncMock(return_value=True)
            )
        )
        self.stack.enter_context(patch.object(plugin, "_apply_personal_model_choice"))
        press = _press(f"model_{plugin.bot_util.sanitize_callback_data('model/b')}")

        asyncio.run(plugin.callback_handler(press))

        (cancel,) = press.edit.await_args.kwargs["buttons"][-1]
        self.assertEqual(tg_compat.button_data(cancel), b"mm:cancel:personal")


class GenericInputHandlerTests(_IsolatedStateTest):
    def setUp(self):
        super().setUp()
        enter = self.stack.enter_context
        enter(patch.object(plugin.llm_chat_config, "load_config", return_value=None))
        enter(
            patch.object(
                plugin, "_guard_model_access", new=AsyncMock(return_value=True)
            )
        )
        self.apply = enter(patch.object(plugin, "_apply_personal_model_choice"))
        self.info = enter(patch.object(plugin, "send_info_message", new=AsyncMock()))

    def test_an_answer_in_the_prompts_topic_is_consumed(self):
        plugin.start_input_flow(_in_topic("/setmodel"), {"type": "model"})
        asyncio.run(plugin.generic_input_handler(_in_topic("provider/custom")))
        self.apply.assert_called_once_with(USER_ID, "provider/custom")
        self.assertNotIn(USER_ID, self.pending)

    def test_a_message_from_another_topic_is_not_consumed(self):
        plugin.start_input_flow(_in_topic("/setmodel"), {"type": "model"})
        asyncio.run(plugin.generic_input_handler(_typed_in_all("Hi")))
        self.apply.assert_not_called()
        self.info.assert_not_awaited()
        self.assertIn(USER_ID, self.pending)

    def test_cancel_from_another_topic_does_not_cancel(self):
        plugin.start_input_flow(_in_topic("/setmodel"), {"type": "model"})
        asyncio.run(plugin.generic_input_handler(_typed_in_all("cancel")))
        self.assertIn(USER_ID, self.pending)


class ChatHandlerInterceptTests(_IsolatedStateTest):
    """`chat_handler` stays silent while a message answers a pending flow.

    The unreadable-rich-message check is the first step after the intercept,
    so a message that gets past the intercept shows up as that notice.
    """

    def setUp(self):
        super().setUp()
        enter = self.stack.enter_context
        enter(patch.object(plugin, "_is_unreadable_rich_message", return_value=True))
        self.info = enter(patch.object(plugin, "send_info_message", new=AsyncMock()))

    def reached_past_intercept(self, event) -> bool:
        self.info.reset_mock()
        asyncio.run(plugin.chat_handler(event))
        return self.info.await_count > 0

    def test_without_a_pending_flow_the_message_is_handled(self):
        self.assertTrue(self.reached_past_intercept(_typed_in_all("Hi")))

    def test_a_flow_from_outside_topics_holds_every_message_as_before(self):
        plugin.start_input_flow(_outside_topics("/setmodel"), {"type": "model"})
        self.assertFalse(self.reached_past_intercept(_outside_topics("Hi")))
        self.assertFalse(self.reached_past_intercept(_typed_in_all("Hi")))

    def test_a_topic_flow_holds_only_its_topic(self):
        plugin.start_input_flow(_in_topic("/setmodel"), {"type": "model"})
        self.assertFalse(self.reached_past_intercept(_in_topic("provider/custom")))
        self.assertTrue(self.reached_past_intercept(_typed_in_all("Hi")))

    def test_the_live_setmodel_case(self):
        """/setmodel in one topic, then "Hi" typed in "All", then the answer.

        Before, "Hi" became the custom model id. Now it is chatted normally,
        and the prompt still takes the answer given in its own topic.
        """
        plugin.start_input_flow(_in_topic("/setmodel", msg_id=400), {"type": "model"})
        hi = _typed_in_all("Hi", msg_id=410)
        self.assertFalse(plugin._answers_pending_input_flow(hi))
        self.assertTrue(self.reached_past_intercept(hi))
        answer = _in_topic("provider/custom", msg_id=411)
        self.assertTrue(plugin._answers_pending_input_flow(answer))
        self.assertFalse(self.reached_past_intercept(answer))

    def test_a_pending_api_key_holds_every_topic(self):
        """Key prompts land in "All", outside topics, so they stay chat-wide."""
        llm_db.AWAITING_KEY_FROM_USERS[USER_ID] = "gemini"
        event = _typed_in_all("Hi")
        self.assertFalse(self.reached_past_intercept(event))
        self.assertTrue(
            plugin._is_pending_input_message(
                event, awaiting=llm_db.is_awaiting_key(event.sender_id)
            )
        )


if __name__ == "__main__":
    unittest.main()
