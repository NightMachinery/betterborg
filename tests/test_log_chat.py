"""Which chat a bot logs to (`Uniborg._resolve_log_chat`, `stdborg`)."""

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from uniborg import uniborg as uniborg_module
from uniborg.uniborg import Uniborg


def _client(*, bot=True, resolvable=()):
    async def get_input_entity(target):
        if target in resolvable:
            return f"entity:{target}"
        raise ValueError(f"cannot resolve {target!r}")

    return SimpleNamespace(
        get_input_entity=AsyncMock(side_effect=get_input_entity),
        is_bot=AsyncMock(return_value=bot),
    )


def _resolve(client, log_chat=None, *, admins=(1, 2, 3)):
    with patch.object(uniborg_module, "admins", list(admins)):
        return asyncio.run(Uniborg._resolve_log_chat(client, log_chat))


class ResolveLogChatTests(unittest.TestCase):
    def test_an_explicit_numeric_chat_is_used(self):
        client = _client(resolvable=(-100,))
        self.assertEqual(_resolve(client, "-100"), "entity:-100")

    def test_a_bot_falls_back_to_the_first_admin_that_resolves(self):
        client = _client(resolvable=(2, 3))
        self.assertEqual(_resolve(client, "-100"), "entity:2")
        self.assertNotIn(
            3, [call.args[0] for call in client.get_input_entity.await_args_list]
        )

    def test_a_bot_without_a_log_chat_does_not_try_one(self):
        client = _client(resolvable=(1,))
        self.assertEqual(_resolve(client), "entity:1")
        self.assertEqual(client.get_input_entity.await_count, 1)

    def test_a_bot_with_no_resolvable_admin_has_no_log_chat(self):
        self.assertIsNone(_resolve(_client()))

    def test_a_userbot_falls_back_to_saved_messages(self):
        client = _client(bot=False, resolvable=("me",))
        self.assertEqual(_resolve(client, "-100"), "entity:me")


if __name__ == "__main__":
    unittest.main()
