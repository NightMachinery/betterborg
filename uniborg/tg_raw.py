# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.
"""Raw Telegram requests that Telethon 1.45 has no helper for.

Guest answers (`answer_guest`) and inline-message edits (`InlineEditor`); see
docs/guest_mode.md. This module imports only the standard library, Telethon
and `telethon_safety`, never ``uniborg.util``, so ``util`` may import modules
that depend on it.
"""
import logging
from typing import Any, Optional

from telethon import errors, functions, types, utils

from uniborg import telethon_safety

_log = logging.getLogger(__name__)

#: Guest answers are inline results; Telegram rejects an article without a
#: title (ARTICLE_TITLE_EMPTY), although the title is never shown in the chat.
GUEST_RESULT_TYPE = "article"


async def send_once(client: Any, request: Any) -> Any:
    """Sends `request` exactly once and returns its result.

    `client(request)` goes through Telethon's `_call`, which re-sends after
    server errors and sleeps through flood waits of up to
    `flood_sleep_threshold` seconds, then sends again. For a request that must
    not run twice, such as a guest answer, that is a second post. This goes to
    the sender directly, and marks the request at-most-once so a reconnect
    fails it with `telethon_safety.DeliveryUnknownError` instead of re-sending
    it (when the safety net is installed).
    """
    await request.resolve(client, utils)
    if not telethon_safety.at_most_once_installed():
        _log.warning(
            "The at-most-once safety net is not installed; a reconnect could "
            "re-send %s",
            type(request).__name__,
        )
    future = client._sender.send(request)
    telethon_safety.mark_at_most_once(future)
    result = await future
    await utils.maybe_async(client.session.process_entities(result))
    return result


def _reply_markup(client: Any, buttons: Any) -> Any:
    if buttons is None:
        return None
    return client.build_reply_markup(buttons)


def _inline_message(
    client: Any,
    *,
    text: Optional[str],
    entities: Optional[list],
    markdown: Optional[str],
    buttons: Any,
    link_preview: bool,
) -> Any:
    if (text is None) == (markdown is None):
        raise ValueError("Pass exactly one of text and markdown")
    reply_markup = _reply_markup(client, buttons)
    if markdown is not None:
        return types.InputBotInlineMessageRichMessage(
            rich_message=types.InputRichMessageMarkdown(markdown=markdown),
            reply_markup=reply_markup,
        )
    return types.InputBotInlineMessageText(
        message=text,
        entities=entities or None,
        no_webpage=not link_preview,
        reply_markup=reply_markup,
    )


async def answer_guest(
    client: Any,
    *,
    query_id: int,
    title: str,
    text: Optional[str] = None,
    entities: Optional[list] = None,
    markdown: Optional[str] = None,
    buttons: Any = None,
    link_preview: bool = False,
) -> Any:
    """Posts the one answer a guest query allows, and returns its inline id.

    Pass plain `text` (with optional `entities`) or rich `markdown`, which
    Telegram's server parses. The answer goes out through `send_once` and is
    never retried: a failure, including `DeliveryUnknownError`, means the
    caller must give up on this query. Returns an `InputBotInlineMessageID`
    (or its 64-bit variant) for `InlineEditor`.
    """
    if not title:
        raise ValueError("A guest answer needs a non-empty title")
    send_message = _inline_message(
        client,
        text=text,
        entities=entities,
        markdown=markdown,
        buttons=buttons,
        link_preview=link_preview,
    )
    result = types.InputBotInlineResult(
        id=str(query_id),
        type=GUEST_RESULT_TYPE,
        title=title,
        send_message=send_message,
    )
    return await send_once(
        client,
        functions.messages.SetBotGuestChatResultRequest(
            query_id=query_id, result=result
        ),
    )


class InlineEditor:
    """Edits one inline message (a guest answer) for the length of a stream.

    Telegram only accepts `messages.editInlineBotMessage` from the data center
    named in the inline id (`MESSAGE_ID_INVALID` otherwise). Telethon's
    `edit_message` routes it, but cannot send `rich_message`, and borrows the
    exported sender inside its `try`, so a failed borrow surfaces as an
    `UnboundLocalError`. This borrows once, on entry, and returns the sender
    on exit.

    Edits are idempotent, so they go through Telethon's `_call`, which sleeps
    through a flood wait of up to `client.flood_sleep_threshold` seconds and
    sends again; a longer one raises `FloodWaitError`.
    """

    def __init__(self, client: Any, inline_id: Any):
        self.client = client
        self.inline_id = inline_id
        self._sender = None

    @property
    def exported(self) -> bool:
        return self.inline_id.dc_id != self.client.session.dc_id

    async def __aenter__(self) -> "InlineEditor":
        if self.exported:
            self._sender = await self.client._borrow_exported_sender(
                self.inline_id.dc_id
            )
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        sender, self._sender = self._sender, None
        if sender is not None:
            await self.client._return_exported_sender(sender)

    async def _invoke(self, request: Any) -> Any:
        if self._sender is not None:
            return await self.client._call(self._sender, request)
        if self.exported:
            raise RuntimeError("InlineEditor used outside its async with block")
        return await self.client(request)

    async def edit(
        self,
        *,
        text: Optional[str] = None,
        entities: Optional[list] = None,
        parse_mode: Any = None,
        markdown: Optional[str] = None,
        buttons: Any = None,
        media: Any = None,
        link_preview: bool = False,
    ) -> bool:
        """Replaces the message's content. Returns False if nothing changed.

        Pass `text` (plain, with `entities`, or parsed with `parse_mode`) or
        rich `markdown`, optionally with `media` (an `InputMedia` of a file
        Telegram already has; inline edits cannot upload) and `buttons`.
        """
        if text is not None and markdown is not None:
            raise ValueError("Pass text or markdown, not both")
        if text is not None and parse_mode is not None:
            if entities is not None:
                raise ValueError("Pass entities or parse_mode, not both")
            text, entities = await self.client._parse_message_text(text, parse_mode)
        request = functions.messages.EditInlineBotMessageRequest(
            id=self.inline_id,
            message=text,
            entities=entities or None,
            no_webpage=not link_preview,
            media=media,
            reply_markup=_reply_markup(self.client, buttons),
            rich_message=(
                types.InputRichMessageMarkdown(markdown=markdown)
                if markdown is not None
                else None
            ),
        )
        try:
            await self._invoke(request)
        except errors.MessageNotModifiedError:
            return False
        return True
