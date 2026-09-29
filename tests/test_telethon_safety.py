import asyncio
import datetime
import importlib.util
import logging
from pathlib import Path
import sys
import unittest

from telethon import errors, functions, types
from telethon._updates import EntityCache, MessageBox
from telethon._updates.messagebox import ENTRY_ACCOUNT

#: Loaded from its file so the `uniborg` package __init__ (which pulls in
#: uniborg.util and, on Telethon 1.45, fails at import) never runs.
_MODULE_PATH = Path(__file__).resolve().parents[1] / "uniborg" / "telethon_safety.py"
_SPEC = importlib.util.spec_from_file_location(
    "telethon_safety_under_test", _MODULE_PATH
)
telethon_safety = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = telethon_safety
_SPEC.loader.exec_module(telethon_safety)

SafetyKind = telethon_safety.SafetyKind
SafetyNet = telethon_safety.SafetyNet

UNKNOWN_ID = 0xDEADBEEF


def _no_env():
    return {}


class _InstalledTestCase(unittest.TestCase):
    def install(self, **kwargs):
        telethon_safety.uninstall_safety_nets()
        self.addCleanup(telethon_safety.uninstall_safety_nets)
        kwargs.setdefault("environ", _no_env())
        return telethon_safety.install_safety_nets(**kwargs)


class EnvSwitchTests(unittest.TestCase):
    def test_unset_or_empty_means_on(self):
        self.assertTrue(telethon_safety.safety_nets_enabled(environ={}))
        self.assertTrue(
            telethon_safety.safety_nets_enabled(environ={"borg_tg_safety_nets": ""})
        )

    def test_recognised_values(self):
        for value in ("1", "true", "Yes", " on "):
            with self.subTest(value=value):
                self.assertTrue(
                    telethon_safety.safety_nets_enabled(
                        environ={"borg_tg_safety_nets": value}
                    )
                )
        for value in ("0", "false", "NO", "off"):
            with self.subTest(value=value):
                self.assertFalse(
                    telethon_safety.safety_nets_enabled(
                        environ={"borg_tg_safety_nets": value}
                    )
                )

    def test_unknown_value_raises(self):
        with self.assertRaises(ValueError):
            telethon_safety.safety_nets_enabled(environ={"borg_tg_safety_nets": "2"})

    def test_disabled_install_changes_nothing(self):
        telethon_safety.uninstall_safety_nets()
        client = _FakeClient({})

        stats = telethon_safety.install_safety_nets(
            client=client, environ={"borg_tg_safety_nets": "0"}
        )

        self.assertEqual(stats.installed, set())
        self.assertIsNone(client.difference_fallback)
        self.assertFalse(telethon_safety.uninstall_safety_nets())


class _FakeTelethonClient:
    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    async def __call__(self, request, ordered=False, flood_sleep_threshold=None):
        self.calls.append(request)
        response = self.responses[type(request)]
        if isinstance(response, Exception):
            raise response
        return response


class _FakeClient(telethon_safety.DifferenceFallbackMixin, _FakeTelethonClient):
    pass


class _Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def _state(pts=500):
    return types.updates.State(
        pts=pts,
        qts=9,
        date=datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc),
        seq=3,
        unread_count=0,
    )


def _difference_request():
    return functions.updates.GetDifferenceRequest(pts=1, date=None, qts=1)


def _channel_difference_request(*, channel_id=77):
    return functions.updates.GetChannelDifferenceRequest(
        channel=types.InputChannel(channel_id=channel_id, access_hash=1),
        filter=types.ChannelMessagesFilterEmpty(),
        pts=10,
        limit=100,
    )


class DifferenceFallbackTests(unittest.TestCase):
    def setUp(self):
        self.clock = _Clock()
        self.sleeps = []
        self.monitor = telethon_safety.SafetyMonitor()
        self.stats = self.monitor.stats

    async def _sleep(self, seconds):
        self.sleeps.append(seconds)

    def _client(self, responses):
        client = _FakeClient(responses)
        client.difference_fallback = telethon_safety.DifferenceFallback(
            report=self.monitor.report, clock=self.clock, sleep=self._sleep
        )
        return client

    def _not_found(self):
        return errors.TypeNotFoundError(UNKNOWN_ID, b"")

    def test_get_difference_returns_an_empty_difference_with_fresh_state(self):
        state = _state()
        client = self._client(
            {
                functions.updates.GetDifferenceRequest: self._not_found(),
                functions.updates.GetStateRequest: state,
            }
        )

        diff = asyncio.run(client(_difference_request()))

        self.assertIsInstance(diff, types.updates.Difference)
        self.assertIs(diff.state, state)
        self.assertEqual(
            (
                diff.new_messages,
                diff.new_encrypted_messages,
                diff.other_updates,
                diff.chats,
                diff.users,
            ),
            ([], [], [], [], []),
        )
        self.assertIsInstance(client.calls[-1], functions.updates.GetStateRequest)
        self.assertEqual(self.stats.by_kind[SafetyKind.DIFFERENCE], 1)
        self.assertEqual(self.stats.by_constructor[UNKNOWN_ID], 1)

    def test_message_box_applies_the_substitute_and_moves_past_the_window(self):
        box = MessageBox(logging.getLogger("test.messagebox"))

        async def get_and_apply_difference():
            #: MessageBox deadlines read the running loop, so stay inside one.
            box.set_state(_state(pts=100))
            box.try_begin_get_diff(ENTRY_ACCOUNT, "test gap")
            request = box.get_difference()
            client = self._client(
                {
                    type(request): self._not_found(),
                    functions.updates.GetStateRequest: _state(pts=500),
                }
            )
            diff = await client(request)
            return box.apply_difference(diff, EntityCache())

        updates, users, chats = asyncio.run(get_and_apply_difference())

        #: Telethon wraps the (empty) other_updates in one Updates batch.
        self.assertEqual(sum(len(batch.updates) for batch in updates), 0)
        self.assertEqual((users, chats), ([], []))
        self.assertEqual(box.map[ENTRY_ACCOUNT].pts, 500)
        self.assertIsNone(box.get_difference())

    def test_channel_difference_raises_channel_private_to_forget_the_channel(self):
        request = _channel_difference_request()
        client = self._client(
            {functions.updates.GetChannelDifferenceRequest: self._not_found()}
        )

        with self.assertRaises(errors.ChannelPrivateError) as caught:
            asyncio.run(client(request))

        self.assertIs(caught.exception.request, request)
        self.assertIsInstance(caught.exception.__cause__, errors.TypeNotFoundError)
        self.assertEqual(self.stats.by_kind[SafetyKind.CHANNEL_DIFFERENCE], 1)

    def test_other_requests_are_counted_and_reraised(self):
        client = self._client({functions.updates.GetStateRequest: self._not_found()})

        with self.assertRaises(errors.TypeNotFoundError):
            asyncio.run(client(functions.updates.GetStateRequest()))

        self.assertEqual(self.stats.by_kind[SafetyKind.RPC_RESULT], 1)

    def test_without_a_fallback_telethon_behaviour_is_kept(self):
        client = _FakeClient(
            {functions.updates.GetDifferenceRequest: self._not_found()}
        )

        with self.assertRaises(errors.TypeNotFoundError):
            asyncio.run(client(_difference_request()))

        self.assertEqual(len(client.calls), 1)

    def test_repeated_fallbacks_back_off_exponentially_then_reset(self):
        client = self._client(
            {
                functions.updates.GetDifferenceRequest: self._not_found(),
                functions.updates.GetStateRequest: _state(),
            }
        )

        async def fall_back(times):
            for _ in range(times):
                await client(_difference_request())
                self.clock.now += 1

        asyncio.run(fall_back(4))
        self.assertEqual(self.sleeps, [1.0, 2.0, 4.0])

        self.clock.now += 3600
        asyncio.run(fall_back(1))
        self.assertEqual(self.sleeps, [1.0, 2.0, 4.0])

    def test_channels_back_off_separately(self):
        client = self._client(
            {functions.updates.GetChannelDifferenceRequest: self._not_found()}
        )

        async def fall_back(channel_ids):
            for channel_id in channel_ids:
                with self.assertRaises(errors.ChannelPrivateError):
                    await client(_channel_difference_request(channel_id=channel_id))

        #: A deadline sweep can fail many channels in a row; each one's first
        #: failure must not wait behind the others.
        asyncio.run(fall_back([1, 2, 3, 4]))
        self.assertEqual(self.sleeps, [])

        asyncio.run(fall_back([2, 2]))
        self.assertEqual(self.sleeps, [1.0, 2.0])

        self.clock.now += 3600
        asyncio.run(fall_back([2]))
        self.assertEqual(self.sleeps, [1.0, 2.0])
        self.assertEqual(len(client.difference_fallback.backoffs), 1)


class InstallWiringTests(_InstalledTestCase):
    def test_install_attaches_the_fallback_to_the_client(self):
        client = _FakeClient({})

        stats = self.install(client=client)

        self.assertIsInstance(
            client.difference_fallback, telethon_safety.DifferenceFallback
        )
        self.assertIn(SafetyNet.DIFFERENCE_FALLBACK, stats.installed)

    def test_install_rejects_a_client_without_the_mixin(self):
        with self.assertRaises(TypeError):
            self.install(client=_FakeTelethonClient({}))


if __name__ == "__main__":
    unittest.main()
