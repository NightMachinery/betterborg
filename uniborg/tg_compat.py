# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.
"""Telegram helpers that work on Telethon 1.43.2 (layer 224) and 1.45.0 (229).

Two button schemas exist (see docs/telethon_upgrade.md):

- the **per-kind schema** (layer 228 and older): one generated class per button
  kind, such as ``KeyboardButtonCallback(text, data)``;
- the **typed schema** (layer 229): ``KeyboardButton(text, type)`` for reply
  keyboards and ``KeyboardInlineButton(text, type)`` for inline ones, where
  ``type`` is a ``ButtonType*`` or ``InlineButtonType*`` object that holds the
  payload.

The builders here always return real generated TL objects, because 1.45's
``build_reply_markup`` silently drops anything else. The readers accept both
schemas plus Telethon's ``Button`` and ``MessageButton`` wrappers.

`TgCapabilities` records which newer Telegram features this Telethon build and
this account can use, so callers gate new paths on a probe instead of a
version number.

This module imports only the standard library and Telethon, so tests and
tools can load it without ``uniborg.util``.
"""
from dataclasses import dataclass, fields
import enum
import inspect
from typing import Any, Awaitable, Callable, Iterable, Optional, Union

import telethon
from telethon.tl import custom, functions, types
from telethon.tl.alltlobjects import LAYER
from telethon.tl.tlobject import TLObject

#: Telegram's limit on inline callback data, in bytes.
CALLBACK_DATA_MAX_BYTES = 64

CallbackData = Union[str, bytes, bytearray, memoryview]


class ButtonSchema(enum.Enum):
    """How the installed Telethon represents keyboard buttons."""

    PER_KIND = "per_kind"
    TYPED = "typed"


class ButtonStyle(str, enum.Enum):
    """Bot API button colours (``KeyboardButtonStyle`` in MTProto)."""

    PRIMARY = "primary"
    SUCCESS = "success"
    DANGER = "danger"


def detect_button_schema(tl_types=None) -> ButtonSchema:
    """Which button schema a generated ``telethon.tl.types`` module uses."""
    tl_types = types if tl_types is None else tl_types
    if hasattr(tl_types, "KeyboardInlineButton"):
        return ButtonSchema.TYPED
    if hasattr(tl_types, "KeyboardButtonCallback"):
        return ButtonSchema.PER_KIND
    raise RuntimeError(
        "Unrecognized Telethon button schema: neither KeyboardInlineButton "
        "nor KeyboardButtonCallback exists"
    )


BUTTON_SCHEMA = detect_button_schema()

#: Both pinned versions have it. Without it the generated button classes take
#: no ``style`` argument, so builders then skip the style. That is not a silent
#: enum fallback: the value is still validated, so an unknown style raises on
#: every version, and a style only colours a button; it never changes what the
#: button sends.
BUTTON_STYLES_SUPPORTED = hasattr(types, "KeyboardButtonStyle")

#: The class of `callback_button` results, for annotations such as
#: ``list[tg_compat.CallbackButton]``. Use `is_callback_button` at runtime.
if BUTTON_SCHEMA is ButtonSchema.TYPED:
    CallbackButton = types.KeyboardInlineButton
elif BUTTON_SCHEMA is ButtonSchema.PER_KIND:
    CallbackButton = types.KeyboardButtonCallback
else:
    raise ValueError(f"Unknown button schema: {BUTTON_SCHEMA!r}")


def _existing_types(*names: str) -> tuple:
    return tuple(getattr(types, name) for name in names if hasattr(types, name))


#: Every generated button, per-kind or typed, has one of these subclass ids.
_BUTTON_SUBCLASS_IDS = frozenset(
    cls.SUBCLASS_OF_ID
    for cls in _existing_types("KeyboardButton", "KeyboardInlineButton")
)
#: What `_payload` returns for URL and request-peer buttons, in either schema.
_URL_PAYLOAD_TYPES = _existing_types("KeyboardButtonUrl", "InlineButtonTypeUrl")
_REQUEST_PEER_PAYLOAD_TYPES = _existing_types(
    "KeyboardButtonRequestPeer",
    "InputKeyboardButtonRequestPeer",
    "ButtonTypeRequestPeer",
    "InputButtonTypeRequestPeer",
)


def _unknown_schema() -> ValueError:
    return ValueError(f"Unknown button schema: {BUTTON_SCHEMA!r}")


def callback_data_bytes(data: CallbackData) -> bytes:
    """Encodes callback data the way Telegram receives it.

    A str becomes its UTF-8 bytes and bytes-like data is kept as is. Telegram
    accepts 1 to 64 bytes, so anything else raises `ValueError` here instead
    of failing at send time.
    """
    if isinstance(data, str):
        raw = data.encode("utf-8")
    elif isinstance(data, (bytes, bytearray, memoryview)):
        raw = bytes(data)
    else:
        raise TypeError(
            f"Callback data must be str or bytes, not {type(data).__name__}"
        )
    if not raw:
        raise ValueError("Callback data must not be empty")
    if len(raw) > CALLBACK_DATA_MAX_BYTES:
        raise ValueError(
            f"Callback data is {len(raw)} bytes; Telegram allows at most "
            f"{CALLBACK_DATA_MAX_BYTES}: {raw!r}"
        )
    return raw


def _style_kwargs(style: Optional[Union[ButtonStyle, str]]) -> dict:
    if style is None:
        return {}
    style = ButtonStyle(style)
    if style is ButtonStyle.PRIMARY:
        flag = "bg_primary"
    elif style is ButtonStyle.SUCCESS:
        flag = "bg_success"
    elif style is ButtonStyle.DANGER:
        flag = "bg_danger"
    else:
        raise ValueError(f"Unknown button style: {style!r}")
    if not BUTTON_STYLES_SUPPORTED:
        return {}
    return {"style": types.KeyboardButtonStyle(**{flag: True})}


def callback_button(
    text: str,
    data: CallbackData,
    *,
    style: Optional[Union[ButtonStyle, str]] = None,
):
    """An inline button that sends ``data`` back as a callback query.

    Unlike ``Button.inline``, empty data is an error rather than a copy of the
    text, and no style object is attached unless ``style`` is given, so the
    wire bytes match a plain ``KeyboardButtonCallback(text, data)``.
    """
    data = callback_data_bytes(data)
    style_kwargs = _style_kwargs(style)
    if BUTTON_SCHEMA is ButtonSchema.TYPED:
        return types.KeyboardInlineButton(
            text, types.InlineButtonTypeCallback(data), **style_kwargs
        )
    if BUTTON_SCHEMA is ButtonSchema.PER_KIND:
        return types.KeyboardButtonCallback(text, data, **style_kwargs)
    raise _unknown_schema()


def url_button(
    text: str,
    url: str,
    *,
    style: Optional[Union[ButtonStyle, str]] = None,
):
    """An inline button that opens ``url``."""
    style_kwargs = _style_kwargs(style)
    if BUTTON_SCHEMA is ButtonSchema.TYPED:
        return types.KeyboardInlineButton(
            text, types.InlineButtonTypeUrl(url), **style_kwargs
        )
    if BUTTON_SCHEMA is ButtonSchema.PER_KIND:
        return types.KeyboardButtonUrl(text, url, **style_kwargs)
    raise _unknown_schema()


def text_button(
    text: str,
    *,
    style: Optional[Union[ButtonStyle, str]] = None,
):
    """A reply-keyboard button that sends its own text as a message."""
    style_kwargs = _style_kwargs(style)
    if BUTTON_SCHEMA is ButtonSchema.TYPED:
        return types.KeyboardButton(text, types.ButtonTypeDefault(), **style_kwargs)
    if BUTTON_SCHEMA is ButtonSchema.PER_KIND:
        return types.KeyboardButton(text, **style_kwargs)
    raise _unknown_schema()


def request_peer_button(
    text: str,
    *,
    button_id: int,
    peer_type,
    max_quantity: int = 1,
    style: Optional[Union[ButtonStyle, str]] = None,
):
    """A reply-keyboard button that asks the user to pick peers.

    ``peer_type`` is a ``RequestPeerType*`` object, for example
    ``types.RequestPeerTypeUser(bot=False)``. The choice arrives as a
    ``MessageActionRequestedPeer*`` service message carrying ``button_id``.
    """
    style_kwargs = _style_kwargs(style)
    if BUTTON_SCHEMA is ButtonSchema.TYPED:
        return types.KeyboardButton(
            text,
            types.ButtonTypeRequestPeer(
                button_id=button_id,
                peer_type=peer_type,
                max_quantity=max_quantity,
            ),
            **style_kwargs,
        )
    if BUTTON_SCHEMA is ButtonSchema.PER_KIND:
        return types.KeyboardButtonRequestPeer(
            text,
            button_id=button_id,
            peer_type=peer_type,
            max_quantity=max_quantity,
            **style_kwargs,
        )
    raise _unknown_schema()


def _unwrap(button):
    if isinstance(button, (custom.Button, custom.MessageButton)):
        return button.button
    return button


def is_generated_button(button) -> bool:
    """Whether ``button`` is a generated TL button that markup keeps."""
    return (
        isinstance(button, TLObject)
        and getattr(button, "SUBCLASS_OF_ID", None) in _BUTTON_SUBCLASS_IDS
    )


def _require_generated_button(button):
    if not is_generated_button(button):
        raise TypeError(
            "Expected a generated Telegram button (from callback_button, "
            f"url_button, text_button or request_peer_button), got {button!r}"
        )
    return button


def is_inline_button(button) -> bool:
    """Whether ``button`` belongs under a message rather than a reply keyboard."""
    #: Telethon's own predicate, the one `build_reply_markup` applies, so rows
    #: built here and rows Telethon builds never disagree.
    return custom.Button._is_inline(_unwrap(button))


def is_callback_button(button) -> bool:
    """Whether ``button`` is an inline button carrying callback data."""
    button = _unwrap(button)
    if BUTTON_SCHEMA is ButtonSchema.TYPED:
        return isinstance(button, types.KeyboardInlineButton) and isinstance(
            button.type, types.InlineButtonTypeCallback
        )
    if BUTTON_SCHEMA is ButtonSchema.PER_KIND:
        return isinstance(button, types.KeyboardButtonCallback)
    raise _unknown_schema()


def button_text(button) -> str:
    """The label of a button, generated or wrapped."""
    text = getattr(_unwrap(button), "text", None)
    if not isinstance(text, str):
        raise TypeError(f"Not a keyboard button: {button!r}")
    return text


def _payload(button):
    #: The object holding kind-specific fields: `.type` in the typed schema,
    #: the button itself in the per-kind one.
    button = _unwrap(button)
    payload = getattr(button, "type", None)
    return button if payload is None else payload


def button_data(button) -> Optional[bytes]:
    """The callback data of a button as bytes, or None for other kinds.

    Reads ``.data`` (per-kind schema) or ``.type.data`` (typed schema). Data
    stored as str, as older call sites built it, comes back UTF-8 encoded.
    """
    data = getattr(_payload(button), "data", None)
    if data is None:
        return None
    if isinstance(data, str):
        return data.encode("utf-8")
    return bytes(data)


def button_data_text(button) -> Optional[str]:
    """`button_data` decoded as UTF-8, or None for non-callback buttons."""
    data = button_data(button)
    return None if data is None else data.decode("utf-8")


def button_url(button) -> Optional[str]:
    """The URL of a URL button, or None for other kinds."""
    payload = _payload(button)
    return payload.url if isinstance(payload, _URL_PAYLOAD_TYPES) else None


@dataclass(frozen=True)
class RequestPeerSpec:
    """What a request-peer button asks the user for."""

    button_id: int
    peer_type: Any
    max_quantity: int


def request_peer_spec(button) -> Optional[RequestPeerSpec]:
    """The request of a request-peer button, or None for other kinds."""
    payload = _payload(button)
    if not isinstance(payload, _REQUEST_PEER_PAYLOAD_TYPES):
        return None
    return RequestPeerSpec(
        button_id=payload.button_id,
        peer_type=payload.peer_type,
        max_quantity=payload.max_quantity,
    )


def button_row(buttons: Iterable):
    """One keyboard row, in the row class the button kinds need.

    Inline buttons go in ``KeyboardInlineButtonRow`` on the typed schema and in
    ``KeyboardButtonRow`` on the per-kind one; reply buttons always go in
    ``KeyboardButtonRow``. A row cannot mix the two.
    """
    buttons = [_require_generated_button(button) for button in buttons]
    if not buttons:
        raise ValueError("A keyboard row needs at least one button")
    inline_flags = {is_inline_button(button) for button in buttons}
    if len(inline_flags) > 1:
        raise ValueError("A keyboard row cannot mix inline and reply buttons")
    (inline,) = inline_flags
    if BUTTON_SCHEMA is ButtonSchema.TYPED:
        row_class = types.KeyboardInlineButtonRow if inline else types.KeyboardButtonRow
    elif BUTTON_SCHEMA is ButtonSchema.PER_KIND:
        row_class = types.KeyboardButtonRow
    else:
        raise _unknown_schema()
    return row_class(buttons)


def _rows(rows: Iterable[Iterable], *, inline: bool) -> list:
    built = [button_row(row) for row in rows]
    for row in built:
        if any(is_inline_button(button) != inline for button in row.buttons):
            expected = "inline" if inline else "reply"
            raise ValueError(f"Only {expected} buttons belong in this keyboard")
    return built


def inline_keyboard(rows: Iterable[Iterable]):
    """A ``ReplyInlineMarkup`` from rows of inline buttons.

    Pass it as ``buttons=`` or ``reply_markup=``; raw requests such as inline
    answers need a markup object rather than nested lists.
    """
    return types.ReplyInlineMarkup(_rows(rows, inline=True))


def reply_keyboard(
    rows: Iterable[Iterable],
    *,
    resize: Optional[bool] = None,
    single_use: Optional[bool] = None,
    selective: Optional[bool] = None,
    persistent: Optional[bool] = None,
    placeholder: Optional[str] = None,
):
    """A ``ReplyKeyboardMarkup`` from rows of reply buttons."""
    return types.ReplyKeyboardMarkup(
        rows=_rows(rows, inline=False),
        resize=resize,
        single_use=single_use,
        selective=selective,
        persistent=persistent,
        placeholder=placeholder,
    )


## Capabilities


@dataclass(frozen=True)
class TgCapabilities:
    """What this Telethon build and the logged-in account can use.

    The schema flags say which generated TL types exist; the account flags
    come from ``get_me()``. A true schema flag means the request can be built,
    not that Telegram accepts it for this account or chat.
    """

    telethon_version: str
    layer: int
    is_bot: bool
    button_schema: ButtonSchema
    button_styles: bool
    #: ``SendMessageTextDraftAction`` exists (layer 224 has the old form).
    draft_text: bool
    #: The draft action takes ``can_stop`` and ``SendMessageStopDraftAction``
    #: exists, so a user can stop a live draft.
    draft_stop: bool
    #: ``InputRichMessageMarkdown`` exists and message sends, edits and inline
    #: edits all take ``rich_message``.
    rich_messages: bool
    rich_drafts: bool
    #: ``UpdateBotGuestChatQuery`` and ``SetBotGuestChatResultRequest`` exist.
    guest_types: bool
    #: ``get_me().bot_guestchat``: Guest Mode is on in BotFather. None when this
    #: Telethon's ``User`` has no such field.
    guest_enabled: Optional[bool]
    #: ``get_me().bot_forum_view``: topics in the bot's private chats.
    private_topics: bool
    #: ``get_me().bot_business``: users can connect the bot to their account.
    business: bool
    #: ``get_me().bot_inline_placeholder`` is set, so inline mode is on.
    inline_mode: bool
    ephemeral: bool
    #: ``MessageEntityFormattedDate`` exists (Bot API ``date_time``).
    formatted_dates: bool

    def describe(self) -> str:
        """One ``name: value`` line per field."""
        return "\n".join(
            f"{field.name}: {_describe_value(getattr(self, field.name))}"
            for field in fields(self)
        )


def _describe_value(value) -> str:
    if value is None:
        return "unknown"
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, enum.Enum):
        return str(value.value)
    return str(value)


def _accepts(cls, parameter: str) -> bool:
    return cls is not None and parameter in inspect.signature(cls.__init__).parameters


def build_capabilities(
    me,
    *,
    tl_types=None,
    tl_functions=None,
    telethon_version: Optional[str] = None,
    layer: Optional[int] = None,
) -> TgCapabilities:
    """Capabilities from a ``get_me()`` result and the generated TL modules.

    The TL modules, version and layer default to the installed Telethon.
    """
    if me is None:
        raise RuntimeError("Capabilities need an authorized client (get_me was None)")
    tl_types = types if tl_types is None else tl_types
    tl_functions = functions if tl_functions is None else tl_functions
    messages = getattr(tl_functions, "messages", None)
    ephemeral = getattr(tl_functions, "ephemeral", None)
    draft_action = getattr(tl_types, "SendMessageTextDraftAction", None)
    return TgCapabilities(
        telethon_version=(
            telethon.__version__ if telethon_version is None else telethon_version
        ),
        layer=LAYER if layer is None else layer,
        is_bot=bool(getattr(me, "bot", False)),
        button_schema=detect_button_schema(tl_types),
        button_styles=hasattr(tl_types, "KeyboardButtonStyle"),
        draft_text=draft_action is not None,
        draft_stop=(
            _accepts(draft_action, "can_stop")
            and hasattr(tl_types, "SendMessageStopDraftAction")
        ),
        rich_messages=(
            hasattr(tl_types, "InputRichMessageMarkdown")
            and all(
                _accepts(getattr(messages, name, None), "rich_message")
                for name in (
                    "SendMessageRequest",
                    "EditMessageRequest",
                    "EditInlineBotMessageRequest",
                )
            )
        ),
        rich_drafts=hasattr(tl_types, "InputSendMessageRichMessageDraftAction"),
        guest_types=(
            hasattr(tl_types, "UpdateBotGuestChatQuery")
            and hasattr(messages, "SetBotGuestChatResultRequest")
        ),
        guest_enabled=(
            bool(me.bot_guestchat) if hasattr(me, "bot_guestchat") else None
        ),
        private_topics=bool(getattr(me, "bot_forum_view", False)),
        business=bool(getattr(me, "bot_business", False)),
        inline_mode=getattr(me, "bot_inline_placeholder", None) is not None,
        ephemeral=hasattr(ephemeral, "SendMessageRequest"),
        formatted_dates=hasattr(tl_types, "MessageEntityFormattedDate"),
    )


async def probe_capabilities(
    client,
    *,
    tl_types=None,
    tl_functions=None,
    telethon_version: Optional[str] = None,
    layer: Optional[int] = None,
) -> TgCapabilities:
    """Probes the account with one ``get_me()`` call. Nothing is cached."""
    return build_capabilities(
        await client.get_me(),
        tl_types=tl_types,
        tl_functions=tl_functions,
        telethon_version=telethon_version,
        layer=layer,
    )


_CAPABILITIES_ATTR = "_tg_capabilities"

ProbeFn = Callable[[Any], Awaitable[TgCapabilities]]


def cached_capabilities(client) -> Optional[TgCapabilities]:
    """The capabilities `capabilities_of` cached on ``client``, if any."""
    return getattr(client, _CAPABILITIES_ATTR, None)


async def capabilities_of(
    client,
    *,
    refresh: bool = False,
    probe: Optional[ProbeFn] = None,
) -> TgCapabilities:
    """The client's capabilities, probed once and then cached on the client.

    Pass ``refresh=True`` to re-probe, for example after toggling Guest Mode in
    BotFather, which changes ``guest_enabled`` without a restart.
    """
    capabilities = None if refresh else cached_capabilities(client)
    if capabilities is None:
        capabilities = await (probe or probe_capabilities)(client)
        setattr(client, _CAPABILITIES_ATTR, capabilities)
    return capabilities


def capabilities_report(
    capabilities: TgCapabilities,
    *,
    safety_stats=None,
) -> str:
    """The ``.tgcaps`` text: every capability, then the safety-net counts."""
    lines = [capabilities.describe()]
    if safety_stats is not None:
        lines.append(f"safety_nets: {safety_stats.summary()}")
    return "\n".join(lines)
