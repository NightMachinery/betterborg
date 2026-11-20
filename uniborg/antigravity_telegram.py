"""Telegram sign-in for the shared Antigravity account profile."""

import asyncio

from cachetools import TTLCache
from telethon import errors, events, types

from uniborg import antigravity, llm_util, tg_compat, username_util
from uniborg.constants import BOT_META_INFO_PREFIX


class TelegramLogin:
    def __init__(self, *, cancel_input, on_connected):
        self.cancel_input = cancel_input
        self.on_connected = on_connected
        self.cards = TTLCache(maxsize=64, ttl=antigravity.LOGIN_TIMEOUT + 30)

    async def start(self, event):
        if not antigravity.private_admin(event):
            return
        user_id = event.sender_id
        if antigravity.linked(user_id):
            await self._send(event, "Google is already connected across Chat, Transcribe, and Imagine.")
            return
        self.cancel_input(user_id)
        is_bot = await event.client.is_bot()
        try:
            link = await antigravity.backend.start_login(user_id)
        except antigravity.AntigravityError as exc:
            await self._send(event, str(exc))
            return

        text = (
            "Sign in with Google in your browser. When it opens a 127.0.0.1 page, "
            "copy the full address and paste it into this same DM. The page may show "
            "a connection error. The link expires in five minutes. "
            "One sign-in works across Chat, Transcribe, and Imagine."
        )
        buttons = None
        if is_bot:
            buttons = [[tg_compat.url_button("Sign in with Google", link.url)]]
        else:
            text = f"{link.url}\n\n{text}"

        # Record the attempt before sending. A callback can arrive while the
        # send is returning, and still needs to edit the original card.
        card = asyncio.get_running_loop().create_future()
        self.cards[user_id] = (link, card)
        message = None
        try:
            message = await self._send(event, text, buttons=buttons)
        finally:
            card.set_result(message)

    async def finish(self, event):
        user_id = event.sender_id
        authorized = antigravity.private_admin(event)
        sign_in = self.cards.get(user_id) if authorized else None
        completion = (
            antigravity.backend.accept_callback(user_id, event.raw_text)
            if sign_in and sign_in[0].callback(event.raw_text) is not None else None
        )
        if completion is not None:
            self.cards.pop(user_id)
        try:
            await event.delete()
        except errors.RPCError:
            pass
        if authorized:
            if completion is not None:
                if await asyncio.shield(completion):
                    note = (
                        "Google connected across Chat, Transcribe, and Imagine. "
                        + self.on_connected(user_id)
                    )
                else:
                    note = "Google sign-in failed. Start again with /antigravity_login."
                message = await asyncio.shield(sign_in[1])
                if message is not None:
                    try:
                        await message.edit(
                            BOT_META_INFO_PREFIX + note,
                            buttons=types.ReplyInlineMarkup(rows=[]),
                            parse_mode=None, link_preview=False,
                        )
                    except errors.RPCError:
                        try:
                            await message.delete()
                        except errors.RPCError:
                            pass
                    else:
                        raise events.StopPropagation
            elif antigravity.linked(user_id):
                note = "Google is already connected across Chat, Transcribe, and Imagine."
            else:
                note = (
                    "Paste the return address in the same bot DM that gave you the link, "
                    "or start again here with /antigravity_login."
                )
            await self._send(event, note)
        raise events.StopPropagation

    def register(self, borg):
        usernames = username_util.active_usernames(getattr(borg, "me", None))
        suffix = rf"(?:@{username_util.username_pattern(usernames)})?"

        async def start(event):
            await self.start(event)

        async def finish(event):
            await self.finish(event)

        # Uniborg removes handlers by plugin module when reloading a plugin.
        start.__module__ = finish.__module__ = self.on_connected.__module__
        borg.on(events.NewMessage(
            pattern=rf"(?i)^/antigravity_login{suffix}\s*$",
            func=lambda event: event.is_private and not event.out,
        ))(start)
        borg.on(events.NewMessage(func=lambda event: (
            event.is_private and not event.out and antigravity.is_callback_url(event.raw_text)
        )))(finish)

    @staticmethod
    async def _send(event, text, **kwargs):
        return await llm_util.send_info_message(
            event, text, parse_mode=None, link_preview=False, reply_to=False, **kwargs,
        )
