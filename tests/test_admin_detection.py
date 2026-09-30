import asyncio
import datetime
import logging
from types import SimpleNamespace
import unittest

from telethon import types
from telethon._updates import EntityCache

from uniborg import guest_util, util

ADMIN = 195391705
STRANGER = 555
ADMINS = [ADMIN, "adminuser"]
ADMIN_CHATS = ["1353500128"]
NOW = datetime.datetime(2026, 9, 30, 12, 0, tzinfo=datetime.timezone.utc)


def _is_admin(event, *, msg=None):
    return asyncio.run(
        util.isAdmin(event, admins=ADMINS, adminChats=ADMIN_CHATS, msg=msg)
    )


def _user(user_id, *, username=None, is_self=False):
    return types.User(id=user_id, username=username, is_self=is_self, first_name="U")


def _event(*, sender, chat, message=None):
    async def get_chat():
        return chat

    return SimpleNamespace(
        message=message or SimpleNamespace(sender=sender, out=False),
        sender=sender,
        sender_id=sender.id,
        get_chat=get_chat,
    )


def _guest_query(*, caller, peer, out=True):
    trigger = types.Message(
        id=10,
        peer_id=peer,
        date=NOW,
        message="@bot hi",
        from_id=types.PeerUser(caller),
        out=out,
    )
    client = SimpleNamespace(_self_id=999, _mb_entity_cache=EntityCache())
    update = SimpleNamespace(query_id=1, message=trigger, reference_messages=[])
    update._entities = {caller: _user(caller)}
    return guest_util.guest_query_from_update(update, client=client)


class _Quiet(unittest.TestCase):
    def setUp(self):
        self._borg = util.borg
        util.borg = SimpleNamespace(
            _logger=logging.getLogger("test.admin"), me=_user(999, is_self=True)
        )
        self.addCleanup(setattr, util, "borg", self._borg)


class GuestAdminTests(_Quiet):
    def test_an_echoed_guest_answer_is_never_admin(self):
        me = _user(999, is_self=True)
        echo = SimpleNamespace(
            sender=me, out=True, guestchat_via_from=types.PeerUser(ADMIN)
        )

        self.assertFalse(_is_admin(_event(sender=me, chat=None, message=echo)))

    def test_an_outgoing_private_trigger_from_a_stranger_is_not_admin(self):
        query = _guest_query(caller=STRANGER, peer=types.PeerUser(ADMIN))

        self.assertFalse(_is_admin(guest_util.GuestEvent(query)))
        self.assertFalse(_is_admin(None, msg=query.trigger))

    def test_an_admin_caller_is_admin_in_a_guest_chat(self):
        query = _guest_query(caller=ADMIN, peer=types.PeerUser(STRANGER))

        self.assertTrue(_is_admin(guest_util.GuestEvent(query)))
        self.assertTrue(_is_admin(None, msg=query.trigger))

    def test_a_guest_trigger_in_an_admin_chat_does_not_vouch(self):
        query = _guest_query(caller=STRANGER, peer=types.PeerChannel(1353500128))

        self.assertFalse(_is_admin(guest_util.GuestEvent(query)))


class RegularAdminTests(_Quiet):
    def test_a_private_chat_with_an_admin_does_not_vouch_for_the_other_side(self):
        stranger = _user(STRANGER)
        #: A userbot's DM: the "chat" is the other party, here an admin.
        chat = _user(ADMIN, username="adminuser")

        self.assertFalse(_is_admin(_event(sender=stranger, chat=chat)))

    def test_admins_and_self_keep_their_results(self):
        admin = _user(ADMIN)
        me = _user(999, is_self=True)
        stranger = _user(STRANGER)

        self.assertTrue(_is_admin(_event(sender=admin, chat=admin)))
        self.assertTrue(_is_admin(_event(sender=me, chat=stranger)))
        self.assertFalse(_is_admin(_event(sender=stranger, chat=stranger)))

    def test_admin_chats_still_vouch_for_their_members(self):
        stranger = _user(STRANGER)
        group = types.Channel(
            id=1353500128,
            title="g",
            photo=types.ChatPhotoEmpty(),
            date=NOW,
        )

        self.assertTrue(_is_admin(_event(sender=stranger, chat=group)))

    def test_a_user_listed_in_admin_chats_stays_admin_in_their_dm(self):
        listed = _user(1353500128)

        self.assertTrue(_is_admin(_event(sender=listed, chat=listed)))

    def test_outgoing_messages_are_still_admin(self):
        stranger = _user(STRANGER)
        message = SimpleNamespace(sender=stranger, out=True)

        self.assertTrue(_is_admin(_event(sender=stranger, chat=None, message=message)))


class AdminCmdTests(_Quiet):
    def test_admin_commands_ignore_echoed_guest_answers(self):
        builder = util.admin_cmd(r"^\.x$")
        echo = SimpleNamespace(out=True, guestchat_via_from=types.PeerUser(ADMIN))

        self.assertFalse(builder.func(SimpleNamespace(message=echo)))
        self.assertTrue(
            builder.func(SimpleNamespace(message=SimpleNamespace(out=True)))
        )


if __name__ == "__main__":
    unittest.main()
