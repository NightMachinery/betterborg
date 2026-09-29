import asyncio
import dataclasses
import importlib.util
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
import unittest
from unittest import mock

import telethon
from telethon import Button, TelegramClient
from telethon.extensions import BinaryReader
from telethon.sessions import MemorySession
from telethon.tl import custom, types
from telethon.tl.alltlobjects import LAYER

#: Loaded from its file so the `uniborg` package __init__, which pulls in
#: uniborg.util and every plugin dependency, never runs: the module must work
#: without them.
_MODULE_PATH = Path(__file__).resolve().parents[1] / "uniborg" / "tg_compat.py"
_SPEC = importlib.util.spec_from_file_location("tg_compat_under_test", _MODULE_PATH)
tg_compat = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = tg_compat
_SPEC.loader.exec_module(tg_compat)

ButtonSchema = tg_compat.ButtonSchema
ButtonStyle = tg_compat.ButtonStyle

TYPED = tg_compat.BUTTON_SCHEMA is ButtonSchema.TYPED


def _roundtrip(obj):
    """Serializes a TL object and parses it back, as Telegram would see it."""
    return BinaryReader(bytes(obj)).tgread_object()


def _client():
    #: Never connected: only `build_reply_markup` is exercised.
    return TelegramClient(MemorySession(), 1, "x")


class SchemaDetectionTest(unittest.TestCase):
    def test_installed_schema_matches_layer(self):
        self.assertEqual(TYPED, LAYER >= 229)
        self.assertEqual(tg_compat.BUTTON_SCHEMA, tg_compat.detect_button_schema(types))

    def test_detects_each_schema_from_fake_modules(self):
        self.assertIs(
            tg_compat.detect_button_schema(
                SimpleNamespace(KeyboardInlineButton=object)
            ),
            ButtonSchema.TYPED,
        )
        self.assertIs(
            tg_compat.detect_button_schema(
                SimpleNamespace(KeyboardButtonCallback=object)
            ),
            ButtonSchema.PER_KIND,
        )

    def test_unknown_schema_raises(self):
        with self.assertRaises(RuntimeError):
            tg_compat.detect_button_schema(SimpleNamespace())

    def test_import_is_light(self):
        #: A fresh interpreter, since other test modules import uniborg.util.
        script = (
            "import importlib.util, sys\n"
            f"spec = importlib.util.spec_from_file_location('m', {str(_MODULE_PATH)!r})\n"
            "spec.loader.exec_module(importlib.util.module_from_spec(spec))\n"
            "print(sorted(name for name in sys.modules if name.startswith('uniborg')))\n"
        )
        result = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            check=True,
        )
        self.assertEqual(result.stdout.strip(), "[]")


class CallbackDataTest(unittest.TestCase):
    def test_str_becomes_utf8_bytes(self):
        self.assertEqual(tg_compat.callback_data_bytes("cu:p:0"), b"cu:p:0")
        self.assertEqual(tg_compat.callback_data_bytes("é"), "é".encode("utf-8"))

    def test_bytes_like_is_kept(self):
        self.assertEqual(tg_compat.callback_data_bytes(b"cu:no"), b"cu:no")
        self.assertEqual(tg_compat.callback_data_bytes(bytearray(b"ab")), b"ab")
        self.assertEqual(tg_compat.callback_data_bytes(memoryview(b"ab")), b"ab")
        self.assertIs(type(tg_compat.callback_data_bytes(bytearray(b"ab"))), bytes)

    def test_limit_is_64_utf8_bytes(self):
        self.assertEqual(len(tg_compat.callback_data_bytes("x" * 64)), 64)
        self.assertEqual(len(tg_compat.callback_data_bytes("é" * 32)), 64)
        for data in ("x" * 65, "é" * 33, b"x" * 65):
            with self.subTest(data=data), self.assertRaises(ValueError):
                tg_compat.callback_data_bytes(data)
        with self.assertRaises(ValueError):
            tg_compat.callback_button("t", "é" * 33)

    def test_empty_and_wrong_types_raise(self):
        for data in ("", b""):
            with self.subTest(data=data), self.assertRaises(ValueError):
                tg_compat.callback_data_bytes(data)
        for data in (None, 5, ["a"]):
            with self.subTest(data=data), self.assertRaises(TypeError):
                tg_compat.callback_data_bytes(data)


class ButtonBuilderTest(unittest.TestCase):
    def test_callback_button_is_generated_and_readable(self):
        for data, expected in (("cu:p:1", b"cu:p:1"), (b"cu:no", b"cu:no")):
            with self.subTest(data=data):
                button = tg_compat.callback_button("Next", data)
                self.assertTrue(tg_compat.is_generated_button(button))
                self.assertIsInstance(button, tg_compat.CallbackButton)
                self.assertTrue(tg_compat.is_callback_button(button))
                self.assertTrue(tg_compat.is_inline_button(button))
                self.assertEqual(tg_compat.button_text(button), "Next")
                self.assertEqual(tg_compat.button_data(button), expected)
                self.assertEqual(
                    tg_compat.button_data_text(button), expected.decode("utf-8")
                )
                self.assertIsNone(tg_compat.button_url(button))
                self.assertIsNone(tg_compat.request_peer_spec(button))
                self.assertIsNone(button.style)

    def test_callback_button_class_per_schema(self):
        button = tg_compat.callback_button("t", "d")
        if TYPED:
            self.assertIs(type(button), types.KeyboardInlineButton)
            self.assertIsInstance(button.type, types.InlineButtonTypeCallback)
            self.assertEqual(button.type.data, b"d")
        else:
            self.assertIs(type(button), types.KeyboardButtonCallback)
            self.assertEqual(button.data, b"d")

    def test_callback_button_wire_bytes_match_plain_constructor(self):
        button = tg_compat.callback_button("Back", "cu:p:0")
        if TYPED:
            plain = types.KeyboardInlineButton(
                "Back", types.InlineButtonTypeCallback(b"cu:p:0")
            )
        else:
            plain = types.KeyboardButtonCallback("Back", "cu:p:0")
        self.assertEqual(bytes(button), bytes(plain))

    def test_url_button(self):
        button = tg_compat.url_button("Docs", "https://example.com")
        self.assertTrue(tg_compat.is_generated_button(button))
        self.assertTrue(tg_compat.is_inline_button(button))
        self.assertFalse(tg_compat.is_callback_button(button))
        self.assertEqual(tg_compat.button_url(button), "https://example.com")
        self.assertIsNone(tg_compat.button_data(button))
        self.assertIsNone(tg_compat.button_data_text(button))
        self.assertEqual(tg_compat.button_text(button), "Docs")

    def test_text_button(self):
        button = tg_compat.text_button("Cancel")
        self.assertIs(type(button), types.KeyboardButton)
        self.assertFalse(tg_compat.is_inline_button(button))
        self.assertFalse(tg_compat.is_callback_button(button))
        self.assertIsNone(tg_compat.button_data(button))
        self.assertIsNone(tg_compat.button_url(button))
        self.assertEqual(tg_compat.button_text(button), "Cancel")
        self.assertIsNone(button.style)
        if TYPED:
            self.assertIsInstance(button.type, types.ButtonTypeDefault)
        else:
            self.assertEqual(bytes(button), bytes(types.KeyboardButton("Cancel")))

    def test_request_peer_button(self):
        peer_type = types.RequestPeerTypeUser(bot=False)
        button = tg_compat.request_peer_button(
            "Choose user", button_id=-123, peer_type=peer_type
        )
        self.assertTrue(tg_compat.is_generated_button(button))
        self.assertFalse(tg_compat.is_inline_button(button))
        self.assertFalse(tg_compat.is_callback_button(button))
        self.assertIsNone(tg_compat.button_data(button))
        spec = tg_compat.request_peer_spec(button)
        self.assertEqual(
            spec,
            tg_compat.RequestPeerSpec(
                button_id=-123, peer_type=peer_type, max_quantity=1
            ),
        )
        parsed = tg_compat.request_peer_spec(_roundtrip(button))
        self.assertEqual(parsed.button_id, -123)
        self.assertEqual(parsed.max_quantity, 1)
        self.assertIsInstance(parsed.peer_type, types.RequestPeerTypeUser)
        self.assertFalse(parsed.peer_type.bot)
        if TYPED:
            self.assertIs(type(button), types.KeyboardButton)
            self.assertIsInstance(button.type, types.ButtonTypeRequestPeer)
        else:
            self.assertIs(type(button), types.KeyboardButtonRequestPeer)

    def test_request_peer_max_quantity(self):
        button = tg_compat.request_peer_button(
            "Pick",
            button_id=1,
            peer_type=types.RequestPeerTypeUser(),
            max_quantity=3,
        )
        self.assertEqual(tg_compat.request_peer_spec(button).max_quantity, 3)

    def test_builders_agree_with_telethon_inline_predicate(self):
        buttons = [
            tg_compat.callback_button("a", "a"),
            tg_compat.url_button("u", "https://example.com"),
            tg_compat.text_button("t"),
            tg_compat.request_peer_button(
                "p", button_id=1, peer_type=types.RequestPeerTypeUser()
            ),
        ]
        for button in buttons:
            with self.subTest(button=button):
                self.assertEqual(
                    tg_compat.is_inline_button(button),
                    custom.Button._is_inline(button),
                )


class ButtonStyleTest(unittest.TestCase):
    def _builders(self, style):
        return [
            tg_compat.callback_button("a", "a", style=style),
            tg_compat.url_button("u", "https://example.com", style=style),
            tg_compat.text_button("t", style=style),
            tg_compat.request_peer_button(
                "p",
                button_id=1,
                peer_type=types.RequestPeerTypeUser(),
                style=style,
            ),
        ]

    def test_each_style_sets_one_flag(self):
        for style, flag in (
            (ButtonStyle.PRIMARY, "bg_primary"),
            (ButtonStyle.SUCCESS, "bg_success"),
            (ButtonStyle.DANGER, "bg_danger"),
            ("danger", "bg_danger"),
        ):
            for button in self._builders(style):
                with self.subTest(style=style, button=type(button).__name__):
                    self.assertIsInstance(button.style, types.KeyboardButtonStyle)
                    self.assertTrue(getattr(button.style, flag))
                    set_flags = [
                        name
                        for name in ("bg_primary", "bg_success", "bg_danger")
                        if getattr(button.style, name)
                    ]
                    self.assertEqual(set_flags, [flag])
                    self.assertTrue(_roundtrip(button).style.to_dict()[flag])

    def test_unknown_style_raises(self):
        for style in ("blue", "PRIMARY", 3):
            with self.subTest(style=style), self.assertRaises(ValueError):
                tg_compat.callback_button("a", "a", style=style)

    def test_unsupported_telethon_ignores_style_but_still_validates(self):
        with mock.patch.object(tg_compat, "BUTTON_STYLES_SUPPORTED", False):
            for button in self._builders(ButtonStyle.PRIMARY):
                with self.subTest(button=type(button).__name__):
                    self.assertIsNone(button.style)
            with self.assertRaises(ValueError):
                tg_compat.text_button("t", style="blue")


class ButtonReaderTest(unittest.TestCase):
    def test_readers_accept_telethon_wrappers(self):
        inline = Button.inline("Hi", b"hi")
        self.assertEqual(tg_compat.button_data(inline), b"hi")
        self.assertTrue(tg_compat.is_callback_button(inline))

        wrapped_text = Button.text("Hello", resize=True)
        self.assertIsInstance(wrapped_text, custom.Button)
        self.assertEqual(tg_compat.button_text(wrapped_text), "Hello")
        self.assertIsNone(tg_compat.button_data(wrapped_text))
        self.assertFalse(tg_compat.is_inline_button(wrapped_text))

        message_button = custom.MessageButton(
            None, tg_compat.callback_button("M", "m:1"), None, None, 1
        )
        self.assertEqual(tg_compat.button_data(message_button), b"m:1")
        self.assertEqual(tg_compat.button_text(message_button), "M")
        self.assertEqual(message_button.data, tg_compat.button_data(message_button))
        self.assertTrue(tg_compat.is_callback_button(message_button))

    def test_readers_normalize_str_data_from_older_call_sites(self):
        if TYPED:
            legacy = types.KeyboardInlineButton(
                "t", types.InlineButtonTypeCallback("cu:add")
            )
        else:
            legacy = types.KeyboardButtonCallback("t", "cu:add")
        self.assertEqual(tg_compat.button_data(legacy), b"cu:add")
        self.assertEqual(tg_compat.button_data_text(legacy), "cu:add")

    def test_readers_on_parsed_objects(self):
        for button in (
            tg_compat.callback_button("é", "é:1"),
            tg_compat.url_button("u", "https://example.com/é"),
        ):
            with self.subTest(button=type(button).__name__):
                parsed = _roundtrip(button)
                self.assertEqual(
                    tg_compat.button_data(parsed), tg_compat.button_data(button)
                )
                self.assertEqual(
                    tg_compat.button_url(parsed), tg_compat.button_url(button)
                )
                self.assertEqual(tg_compat.button_text(parsed), button.text)

    def test_duck_typed_fakes(self):
        self.assertEqual(tg_compat.button_data(SimpleNamespace(data=b"x")), b"x")
        self.assertEqual(
            tg_compat.button_data(SimpleNamespace(type=SimpleNamespace(data="y"))),
            b"y",
        )
        self.assertIsNone(tg_compat.button_data(SimpleNamespace(text="t")))

    def test_button_text_rejects_non_buttons(self):
        for value in (None, object(), SimpleNamespace(text=5)):
            with self.subTest(value=value), self.assertRaises(TypeError):
                tg_compat.button_text(value)


class RowAndKeyboardTest(unittest.TestCase):
    def test_inline_row_class_per_schema(self):
        row = tg_compat.button_row(
            [
                tg_compat.callback_button("a", "a"),
                tg_compat.url_button("u", "https://example.com"),
            ]
        )
        expected = types.KeyboardInlineButtonRow if TYPED else types.KeyboardButtonRow
        self.assertIs(type(row), expected)
        self.assertEqual(len(row.buttons), 2)

    def test_reply_row_is_keyboard_button_row(self):
        row = tg_compat.button_row((tg_compat.text_button("a"),))
        self.assertIs(type(row), types.KeyboardButtonRow)

    def test_row_rejects_mixed_empty_and_foreign(self):
        with self.assertRaises(ValueError):
            tg_compat.button_row(
                [tg_compat.callback_button("a", "a"), tg_compat.text_button("t")]
            )
        with self.assertRaises(ValueError):
            tg_compat.button_row([])
        for foreign in (SimpleNamespace(text="t", data=b"d"), Button.text("t")):
            with self.subTest(foreign=foreign), self.assertRaises(TypeError):
                tg_compat.button_row([foreign])

    def test_inline_keyboard_serializes_and_round_trips(self):
        markup = tg_compat.inline_keyboard(
            [
                [tg_compat.callback_button("a", "cu:p:0")],
                [
                    tg_compat.callback_button("b", b"cu:no"),
                    tg_compat.url_button("u", "https://example.com"),
                ],
            ]
        )
        self.assertIsInstance(markup, types.ReplyInlineMarkup)
        parsed = _roundtrip(markup)
        flat = [button for row in parsed.rows for button in row.buttons]
        self.assertEqual(
            [tg_compat.button_data(button) for button in flat],
            [b"cu:p:0", b"cu:no", None],
        )
        self.assertEqual(tg_compat.button_url(flat[2]), "https://example.com")

    def test_reply_keyboard_keeps_options_and_serializes(self):
        markup = tg_compat.reply_keyboard(
            [
                [
                    tg_compat.request_peer_button(
                        "Choose user",
                        button_id=-123,
                        peer_type=types.RequestPeerTypeUser(bot=False),
                    )
                ],
                [tg_compat.text_button("Cancel")],
            ],
            resize=True,
            single_use=True,
            placeholder="Pick one",
        )
        self.assertIsInstance(markup, types.ReplyKeyboardMarkup)
        self.assertTrue(markup.resize)
        self.assertTrue(markup.single_use)
        self.assertEqual(markup.placeholder, "Pick one")
        parsed = _roundtrip(markup)
        self.assertEqual(
            tg_compat.request_peer_spec(parsed.rows[0].buttons[0]).button_id, -123
        )
        self.assertEqual(tg_compat.button_text(parsed.rows[1].buttons[0]), "Cancel")

    def test_keyboards_reject_the_wrong_kind(self):
        with self.assertRaises(ValueError):
            tg_compat.inline_keyboard([[tg_compat.text_button("t")]])
        with self.assertRaises(ValueError):
            tg_compat.reply_keyboard([[tg_compat.callback_button("a", "a")]])

    def test_empty_inline_keyboard(self):
        self.assertEqual(tg_compat.inline_keyboard([]).rows, [])


class BuildReplyMarkupTest(unittest.TestCase):
    """Telethon's own markup builder must keep every helper's output."""

    def setUp(self):
        self.client = _client()
        self.assertFalse(self.client.is_connected())

    def test_inline_buttons_survive(self):
        rows = [
            [tg_compat.callback_button("a", "a"), tg_compat.callback_button("b", "b")],
            [tg_compat.url_button("u", "https://example.com", style="primary")],
        ]
        markup = self.client.build_reply_markup(rows)
        self.assertIsInstance(markup, types.ReplyInlineMarkup)
        kept = [button for row in markup.rows for button in row.buttons]
        self.assertEqual(kept, [button for row in rows for button in row])
        if TYPED:
            self.assertTrue(
                all(type(row) is types.KeyboardInlineButtonRow for row in markup.rows)
            )
        self.assertGreater(len(bytes(markup)), 0)

    def test_flat_list_and_single_button_survive(self):
        flat = [
            tg_compat.callback_button("a", "a"),
            tg_compat.callback_button("b", "b"),
        ]
        markup = self.client.build_reply_markup(flat)
        self.assertEqual(markup.rows[0].buttons, flat)
        single = tg_compat.callback_button("s", "s")
        self.assertEqual(
            self.client.build_reply_markup(single).rows[0].buttons, [single]
        )

    def test_reply_buttons_survive(self):
        rows = [
            [
                tg_compat.request_peer_button(
                    "Choose user",
                    button_id=7,
                    peer_type=types.RequestPeerTypeUser(bot=False),
                )
            ],
            [tg_compat.text_button("Cancel", style=ButtonStyle.DANGER)],
        ]
        markup = self.client.build_reply_markup(rows)
        self.assertIsInstance(markup, types.ReplyKeyboardMarkup)
        kept = [button for row in markup.rows for button in row.buttons]
        self.assertEqual(kept, [button for row in rows for button in row])
        self.assertGreater(len(bytes(markup)), 0)

    def test_prebuilt_markup_passes_through(self):
        for markup in (
            tg_compat.inline_keyboard([[tg_compat.callback_button("a", "a")]]),
            tg_compat.reply_keyboard([[tg_compat.text_button("t")]]),
        ):
            with self.subTest(markup=type(markup).__name__):
                self.assertIs(self.client.build_reply_markup(markup), markup)

    @unittest.skipUnless(TYPED, "only the typed schema drops unknown objects")
    def test_typed_schema_drops_foreign_objects_silently(self):
        #: The reason every builder returns a generated instance.
        markup = self.client.build_reply_markup(
            [[SimpleNamespace(text="fake", data=b"fake")]]
        )
        self.assertEqual(markup.rows, [])


def _me(**fields):
    base = dict(
        bot=True,
        bot_forum_view=None,
        bot_business=None,
        bot_inline_placeholder=None,
    )
    base.update(fields)
    return SimpleNamespace(**base)


class _OldDraftAction:
    def __init__(self, text, random_id=None):
        pass


class _NewDraftAction:
    def __init__(self, text, can_stop=None, keep_on_stop=None, random_id=None):
        pass


class _RichRequest:
    def __init__(self, peer, message, rich_message=None):
        pass


class _PlainRequest:
    def __init__(self, peer, message):
        pass


def _fake_schema(*, new):
    """Fake generated TL modules for layer 224 (new=False) or 229 (new=True)."""
    if new:
        tl_types = SimpleNamespace(
            KeyboardInlineButton=object,
            KeyboardButtonStyle=object,
            SendMessageTextDraftAction=_NewDraftAction,
            SendMessageStopDraftAction=object,
            InputRichMessageMarkdown=object,
            InputSendMessageRichMessageDraftAction=object,
            UpdateBotGuestChatQuery=object,
            MessageEntityFormattedDate=object,
        )
        tl_functions = SimpleNamespace(
            messages=SimpleNamespace(
                SendMessageRequest=_RichRequest,
                EditMessageRequest=_RichRequest,
                EditInlineBotMessageRequest=_RichRequest,
                SetBotGuestChatResultRequest=object,
            ),
            ephemeral=SimpleNamespace(SendMessageRequest=object),
        )
    else:
        tl_types = SimpleNamespace(
            KeyboardButtonCallback=object,
            KeyboardButtonStyle=object,
            SendMessageTextDraftAction=_OldDraftAction,
            MessageEntityFormattedDate=object,
        )
        tl_functions = SimpleNamespace(
            messages=SimpleNamespace(
                SendMessageRequest=_PlainRequest,
                EditMessageRequest=_PlainRequest,
                EditInlineBotMessageRequest=_PlainRequest,
            ),
        )
    return dict(tl_types=tl_types, tl_functions=tl_functions)


_SCHEMA_FLAGS = (
    "draft_stop",
    "rich_messages",
    "rich_drafts",
    "guest_types",
    "ephemeral",
)


class CapabilityFlagsTest(unittest.TestCase):
    def test_installed_telethon_flags(self):
        caps = tg_compat.build_capabilities(_me(bot_guestchat=True))
        self.assertEqual(caps.telethon_version, telethon.__version__)
        self.assertEqual(caps.layer, LAYER)
        self.assertIs(caps.button_schema, tg_compat.BUTTON_SCHEMA)
        self.assertTrue(caps.button_styles)
        self.assertTrue(caps.draft_text)
        self.assertTrue(caps.formatted_dates)
        for flag in _SCHEMA_FLAGS:
            with self.subTest(flag=flag):
                self.assertEqual(getattr(caps, flag), TYPED)

    def test_installed_user_class_decides_guest_field(self):
        me = types.User(id=1, bot=True, bot_forum_view=True)
        caps = tg_compat.build_capabilities(me)
        self.assertTrue(caps.is_bot)
        self.assertTrue(caps.private_topics)
        self.assertEqual(caps.guest_enabled, False if TYPED else None)

    def test_old_and_new_fake_schemas(self):
        for new in (False, True):
            with self.subTest(new=new):
                caps = tg_compat.build_capabilities(
                    _me(), telethon_version="9.9", layer=999, **_fake_schema(new=new)
                )
                self.assertEqual(caps.telethon_version, "9.9")
                self.assertEqual(caps.layer, 999)
                self.assertIs(
                    caps.button_schema,
                    ButtonSchema.TYPED if new else ButtonSchema.PER_KIND,
                )
                self.assertTrue(caps.draft_text)
                for flag in _SCHEMA_FLAGS:
                    self.assertEqual(getattr(caps, flag), new, flag)

    def test_rich_needs_every_request_to_take_rich_message(self):
        schema = _fake_schema(new=True)
        schema["tl_functions"].messages.EditInlineBotMessageRequest = _PlainRequest
        caps = tg_compat.build_capabilities(_me(), **schema)
        self.assertFalse(caps.rich_messages)

    def test_draft_stop_needs_the_stop_action(self):
        schema = _fake_schema(new=True)
        del schema["tl_types"].SendMessageStopDraftAction
        caps = tg_compat.build_capabilities(_me(), **schema)
        self.assertTrue(caps.draft_text)
        self.assertFalse(caps.draft_stop)

    def test_account_flags(self):
        schema = _fake_schema(new=True)
        caps = tg_compat.build_capabilities(
            _me(
                bot_guestchat=True,
                bot_forum_view=True,
                bot_business=True,
                bot_inline_placeholder="",
            ),
            **schema,
        )
        self.assertTrue(caps.is_bot)
        self.assertTrue(caps.guest_enabled)
        self.assertTrue(caps.private_topics)
        self.assertTrue(caps.business)
        self.assertTrue(caps.inline_mode)

        user = tg_compat.build_capabilities(_me(bot=None, bot_guestchat=None), **schema)
        self.assertFalse(user.is_bot)
        self.assertIs(user.guest_enabled, False)
        self.assertFalse(user.private_topics)
        self.assertFalse(user.business)
        self.assertFalse(user.inline_mode)

    def test_guest_enabled_unknown_without_the_field(self):
        caps = tg_compat.build_capabilities(_me(), **_fake_schema(new=False))
        self.assertIsNone(caps.guest_enabled)

    def test_unauthorized_client_raises(self):
        with self.assertRaises(RuntimeError):
            tg_compat.build_capabilities(None)

    def test_frozen(self):
        caps = tg_compat.build_capabilities(_me())
        with self.assertRaises(dataclasses.FrozenInstanceError):
            caps.is_bot = False

    def test_describe_lists_every_field(self):
        caps = tg_compat.build_capabilities(
            _me(), telethon_version="1.45.0", layer=229, **_fake_schema(new=True)
        )
        lines = caps.describe().splitlines()
        self.assertEqual(
            [line.split(":", 1)[0] for line in lines],
            [field.name for field in dataclasses.fields(caps)],
        )
        self.assertIn("telethon_version: 1.45.0", lines)
        self.assertIn("layer: 229", lines)
        self.assertIn("button_schema: typed", lines)
        self.assertIn("draft_stop: yes", lines)
        self.assertIn("business: no", lines)
        old = tg_compat.build_capabilities(_me(), **_fake_schema(new=False))
        self.assertIn("guest_enabled: unknown", old.describe().splitlines())

    def test_report_appends_safety_stats_when_present(self):
        caps = tg_compat.build_capabilities(_me())
        self.assertEqual(tg_compat.capabilities_report(caps), caps.describe())
        stats = SimpleNamespace(summary=lambda: "nets: none; events: none")
        report = tg_compat.capabilities_report(caps, safety_stats=stats)
        self.assertEqual(
            report.splitlines()[-1], "safety_nets: nets: none; events: none"
        )
        self.assertTrue(report.startswith(caps.describe()))


class _FakeClient:
    def __init__(self, me):
        self.me = me
        self.get_me_calls = 0

    async def get_me(self):
        self.get_me_calls += 1
        return self.me


class CapabilityProbeTest(unittest.TestCase):
    def test_probe_calls_get_me_once(self):
        client = _FakeClient(_me(bot_guestchat=True))
        caps = asyncio.run(
            tg_compat.probe_capabilities(client, **_fake_schema(new=True))
        )
        self.assertEqual(client.get_me_calls, 1)
        self.assertTrue(caps.guest_enabled)
        self.assertIsNone(tg_compat.cached_capabilities(client))

    def test_capabilities_are_cached_on_the_client(self):
        client = _FakeClient(_me())
        self.assertIsNone(tg_compat.cached_capabilities(client))
        first = asyncio.run(tg_compat.capabilities_of(client))
        second = asyncio.run(tg_compat.capabilities_of(client))
        self.assertIs(first, second)
        self.assertIs(tg_compat.cached_capabilities(client), first)
        self.assertEqual(client.get_me_calls, 1)

    def test_refresh_reprobes(self):
        client = _FakeClient(_me(bot_forum_view=False))
        first = asyncio.run(tg_compat.capabilities_of(client))
        client.me = _me(bot_forum_view=True)
        refreshed = asyncio.run(tg_compat.capabilities_of(client, refresh=True))
        self.assertEqual(client.get_me_calls, 2)
        self.assertFalse(first.private_topics)
        self.assertTrue(refreshed.private_topics)
        self.assertIs(tg_compat.cached_capabilities(client), refreshed)

    def test_probe_is_injectable(self):
        client = SimpleNamespace()
        expected = tg_compat.build_capabilities(_me())
        calls = []

        async def probe(target):
            calls.append(target)
            return expected

        self.assertIs(
            asyncio.run(tg_compat.capabilities_of(client, probe=probe)), expected
        )
        self.assertEqual(calls, [client])


if __name__ == "__main__":
    unittest.main()
