import asyncio
import builtins
import importlib
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


class _FakeBorg:
    def on(self, *args, **kwargs):
        def decorator(func):
            return func

        return decorator


builtins.borg = _FakeBorg()
all_plugin = importlib.import_module("stdplugins.all")


async def _async_participants(users):
    for user in users:
        yield user


def _user(user_id, *, bot=False, is_self=False, first_name=None, last_name=None, username=None):
    return SimpleNamespace(
        id=user_id,
        bot=bot,
        is_self=is_self,
        first_name=first_name,
        last_name=last_name,
        username=username,
    )


def _event():
    return SimpleNamespace(chat_id=123)


def _collect_messages(mode, users):
    async def collect():
        return [
            message
            async for message in all_plugin._iter_mention_messages(
                mode, _event(), _async_participants(users)
            )
        ]

    return asyncio.run(collect())


class AllMentionsTests(unittest.TestCase):
    def test_human_participant_is_included(self):
        messages = _collect_messages("all", [_user(1)])

        self.assertEqual(messages, ["@all\n[⁣](tg://user?id=1)"])

    def test_bot_participant_is_skipped(self):
        messages = _collect_messages("all", [_user(1, bot=True), _user(2)])

        self.assertEqual(messages, ["@all\n[⁣](tg://user?id=2)"])

    def test_self_participant_is_skipped(self):
        messages = _collect_messages("all", [_user(1, is_self=True), _user(2)])

        self.assertEqual(messages, ["@all\n[⁣](tg://user?id=2)"])

    def test_skipped_participants_do_not_consume_batch_limit(self):
        users = [_user(index, username=str(index)) for index in range(1, 5)]
        users.insert(2, _user(99, bot=True))

        messages = _collect_messages("allf", users)

        self.assertEqual(
            messages,
            [
                "[@1](tg://user?id=1)\n"
                "[@2](tg://user?id=2)\n"
                "[@3](tg://user?id=3)\n"
                "[@4](tg://user?id=4)\n"
            ],
        )

    def test_filter_applies_to_all_modes(self):
        users = [
            _user(1, bot=True, username="bot"),
            _user(2, is_self=True, username="self"),
            _user(3, username="human", first_name="Human", last_name="User"),
        ]

        self.assertEqual(_collect_messages("all", []), [])
        self.assertEqual(_collect_messages("all", users), ["@all\n[⁣](tg://user?id=3)"])
        self.assertEqual(_collect_messages("allf", users), ["[@human](tg://user?id=3)\n"])
        self.assertEqual(
            _collect_messages("allids", users),
            ["Users in chat number 123:\nHuman User (human): id=3\n"],
        )


if __name__ == "__main__":
    unittest.main()
