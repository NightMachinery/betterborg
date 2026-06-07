import asyncio
import builtins
import importlib
import unittest
from types import SimpleNamespace

from telethon.tl.types import (
    MessageReactions,
    PeerChannel,
    ReactionCount,
    ReactionEmoji,
    UpdateMessageReactions,
    Updates,
)


class _FakeLoop:
    def create_task(self, coro):
        coro.close()
        return None


class _FakeBorg:
    loop = _FakeLoop()


_llm_chat = None


def _load_llm_chat():
    global _llm_chat
    if _llm_chat is not None:
        return _llm_chat

    builtins.borg = _FakeBorg()

    async def _import_module():
        return importlib.import_module("llm_chat_plugins.llm_chat")

    _llm_chat = asyncio.run(_import_module())
    return _llm_chat


def _reaction_payload(emoji="👍", count=1):
    return MessageReactions(
        results=[ReactionCount(reaction=ReactionEmoji(emoji), count=count)]
    )


class _FakeClient:
    def __init__(self, updates=None, fail_refresh=False):
        self.updates = updates if updates is not None else []
        self.fail_refresh = fail_refresh
        self.requests = []

    async def get_input_entity(self, chat_id):
        self.chat_id = chat_id
        return f"input-{chat_id}"

    async def __call__(self, request):
        self.requests.append(request)
        if self.fail_refresh:
            raise RuntimeError("telegram unavailable")
        return self.updates


class ReactionHydrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.llm_chat = _load_llm_chat()

    def test_refresh_populates_matching_message_reactions(self):
        reactions = _reaction_payload("❤️", 2)
        messages = [
            SimpleNamespace(id=1209, reactions=None),
            SimpleNamespace(id=1210, reactions=None),
        ]
        client = _FakeClient(
            Updates(
                updates=[
                    UpdateMessageReactions(
                        peer=PeerChannel(123), msg_id=1210, reactions=reactions
                    )
                ],
                users=[],
                chats=[],
                date=None,
                seq=0,
            )
        )

        asyncio.run(self.llm_chat._refresh_message_reactions(client, 123, messages))

        self.assertIsNone(messages[0].reactions)
        self.assertIs(messages[1].reactions, reactions)
        self.assertEqual(client.requests[0].id, [1209, 1210])

    def test_refresh_accepts_single_update_response(self):
        reactions = _reaction_payload("🔥", 1)
        message = SimpleNamespace(id=1209, reactions=None)
        client = _FakeClient(
            UpdateMessageReactions(
                peer=PeerChannel(123), msg_id=1209, reactions=reactions
            )
        )

        asyncio.run(self.llm_chat._refresh_message_reactions(client, 123, [message]))

        self.assertIs(message.reactions, reactions)

    def test_refresh_failure_leaves_messages_unchanged(self):
        message = SimpleNamespace(id=1209, reactions=None)
        client = _FakeClient(fail_refresh=True)

        asyncio.run(self.llm_chat._refresh_message_reactions(client, 123, [message]))

        self.assertIsNone(message.reactions)
        self.assertEqual(len(client.requests), 1)


if __name__ == "__main__":
    unittest.main()
