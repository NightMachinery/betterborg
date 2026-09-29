import asyncio
import datetime
import importlib.util
import logging
from pathlib import Path
import struct
import sys
from types import SimpleNamespace
import unittest

import telethon
from telethon import errors, functions, types
from telethon._updates import EntityCache, MessageBox
from telethon._updates.messagebox import ENTRY_ACCOUNT
from telethon.extensions import BinaryReader
from telethon.network.mtprotosender import MTProtoSender
from telethon.tl.core import GzipPacked, MessageContainer, TLMessage

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
SENDER_LOGGER = "telethon.network.mtprotosender"


def _entry(msg_id, body, *, seq_no=0):
    return struct.pack("<qii", msg_id, seq_no, len(body)) + body


def _container(*entries):
    return struct.pack("<Ii", MessageContainer.CONSTRUCTOR_ID, len(entries)) + b"".join(
        entries
    )


def _update_short_with_unknown_update():
    #: updateShort#78d4dec1 update:Update date:int, where the Update is a
    #: constructor Telethon does not know, followed by bytes it cannot size.
    return struct.pack("<II", types.UpdateShort.CONSTRUCTOR_ID, UNKNOWN_ID) + bytes(12)


def _gzip_packed_unknown():
    return bytes(GzipPacked(struct.pack("<I", UNKNOWN_ID) + bytes(8)))


def _pong(*, msg_id, ping_id=7):
    return bytes(types.Pong(msg_id=msg_id, ping_id=ping_id))


class _Loggers(dict):
    def __missing__(self, key):
        return logging.getLogger(key)


def _no_env():
    return {}


class _InstalledTestCase(unittest.TestCase):
    def install(self, **kwargs):
        telethon_safety.uninstall_safety_nets()
        self.addCleanup(telethon_safety.uninstall_safety_nets)
        kwargs.setdefault("environ", _no_env())
        return telethon_safety.install_safety_nets(**kwargs)


class ContainerSkipTests(_InstalledTestCase):
    def test_without_the_patch_one_unknown_entry_sinks_the_container(self):
        telethon_safety.uninstall_safety_nets()
        data = _container(
            _entry(101, _update_short_with_unknown_update()),
            _entry(103, _pong(msg_id=55)),
        )

        with self.assertRaises(errors.TypeNotFoundError):
            BinaryReader(data).tgread_object()

    def test_unknown_entry_becomes_a_placeholder_and_the_pong_survives(self):
        stats = self.install()
        data = _container(
            _entry(101, _update_short_with_unknown_update()),
            _entry(103, _pong(msg_id=55)),
        )
        reader = BinaryReader(data)

        container = reader.tgread_object()

        self.assertIsInstance(container, MessageContainer)
        self.assertEqual([m.msg_id for m in container.messages], [101, 103])
        skipped, pong = (m.obj for m in container.messages)
        self.assertIsInstance(skipped, telethon_safety.SkippedObject)
        self.assertEqual(skipped.outer_constructor_id, types.UpdateShort.CONSTRUCTOR_ID)
        self.assertEqual(skipped.invalid_constructor_id, UNKNOWN_ID)
        self.assertNotEqual(skipped.SUBCLASS_OF_ID, 0x8AF52AAC)
        self.assertIsInstance(pong, types.Pong)
        self.assertEqual(pong.msg_id, 55)
        self.assertEqual(reader.tell_position(), len(data))
        self.assertEqual(stats.by_kind[SafetyKind.CONTAINER_ENTRY], 1)
        self.assertEqual(stats.by_constructor[UNKNOWN_ID], 1)

    def test_sender_acks_every_entry_and_keeps_siblings_after_a_gzip_failure(self):
        stats = self.install()
        data = _container(
            _entry(101, _update_short_with_unknown_update()),
            _entry(103, _gzip_packed_unknown()),
            _entry(105, _pong(msg_id=55)),
        )
        container = BinaryReader(data).tgread_object()

        async def process():
            queue = asyncio.Queue()
            sender = MTProtoSender(None, loggers=_Loggers(), updates_queue=queue)
            pong_future = asyncio.get_running_loop().create_future()
            sender._pending_state[55] = SimpleNamespace(future=pong_future)
            await sender._process_message(TLMessage(99, 0, container))
            return sender, queue, pong_future

        sender, queue, pong_future = asyncio.run(process())

        self.assertEqual(sender._pending_ack, {99, 101, 103, 105})
        self.assertTrue(pong_future.done())
        self.assertIsInstance(pong_future.result(), types.Pong)
        self.assertTrue(queue.empty())
        self.assertEqual(stats.by_kind[SafetyKind.CONTAINER_ENTRY], 1)
        self.assertEqual(stats.by_kind[SafetyKind.PROCESSING], 1)
        self.assertEqual(stats.by_constructor[UNKNOWN_ID], 2)

    def test_install_is_idempotent_and_uninstall_restores_telethon(self):
        original_reader = MessageContainer.__dict__["from_reader"]
        original_process = MTProtoSender.__dict__["_process_message"]
        stats = self.install()
        patched_reader = MessageContainer.__dict__["from_reader"]

        again = telethon_safety.install_safety_nets(environ=_no_env())

        self.assertIs(again, stats)
        self.assertIs(MessageContainer.__dict__["from_reader"], patched_reader)
        self.assertIsNot(patched_reader, original_reader)
        self.assertEqual(
            stats.installed,
            {SafetyNet.CONTAINER_SKIP, SafetyNet.MESSAGE_GUARD, SafetyNet.LOG_COUNTER},
        )
        handlers = logging.getLogger(SENDER_LOGGER).handlers
        self.assertEqual(
            sum(
                isinstance(h, telethon_safety.TypeNotFoundLogHandler) for h in handlers
            ),
            1,
        )

        self.assertTrue(telethon_safety.uninstall_safety_nets())

        self.assertIs(MessageContainer.__dict__["from_reader"], original_reader)
        self.assertIs(MTProtoSender.__dict__["_process_message"], original_process)
        self.assertFalse(
            any(
                isinstance(h, telethon_safety.TypeNotFoundLogHandler)
                for h in logging.getLogger(SENDER_LOGGER).handlers
            )
        )


class VersionGuardTests(unittest.TestCase):
    def test_unknown_version_skips_the_patches_with_a_warning(self):
        container_cls = type("FakeContainer", (), {"from_reader": classmethod(id)})
        sender_cls = type("FakeSender", (), {"_process_message": id})
        fake = SimpleNamespace(
            __name__="fake_telethon",
            __version__="1.99.0",
            errors=errors,
            tl=SimpleNamespace(
                core=SimpleNamespace(
                    messagecontainer=SimpleNamespace(MessageContainer=container_cls)
                )
            ),
            network=SimpleNamespace(
                mtprotosender=SimpleNamespace(MTProtoSender=sender_cls)
            ),
        )
        self.addCleanup(telethon_safety.uninstall_safety_nets, telethon_module=fake)

        with self.assertLogs(telethon_safety.__name__, logging.WARNING) as logs:
            stats = telethon_safety.install_safety_nets(
                telethon_module=fake, environ=_no_env()
            )

        self.assertIn("1.99.0", "\n".join(logs.output))
        self.assertEqual(stats.installed, {SafetyNet.LOG_COUNTER})
        self.assertIs(container_cls.__dict__["from_reader"].__func__, id)
        self.assertIs(sender_cls.__dict__["_process_message"], id)

    def test_supported_versions_include_the_installed_telethon(self):
        self.assertIn(telethon.__version__, telethon_safety.SUPPORTED_TELETHON_VERSIONS)


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
        original_reader = MessageContainer.__dict__["from_reader"]
        client = _FakeClient({})

        stats = telethon_safety.install_safety_nets(
            client=client, environ={"borg_tg_safety_nets": "0"}
        )

        self.assertEqual(stats.installed, set())
        self.assertIsNone(client.difference_fallback)
        self.assertIs(MessageContainer.__dict__["from_reader"], original_reader)
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
        self.alerts = []
        self.monitor = telethon_safety.SafetyMonitor(
            alert=self._alert, clock=self.clock
        )
        self.stats = self.monitor.stats

    async def _alert(self, text):
        self.alerts.append(text)

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

    def test_alerts_are_rate_limited_per_kind(self):
        client = self._client(
            {
                functions.updates.GetDifferenceRequest: self._not_found(),
                functions.updates.GetStateRequest: _state(),
            }
        )

        async def fall_back(times):
            for _ in range(times):
                await client(_difference_request())
            await asyncio.sleep(0)

        asyncio.run(fall_back(3))
        self.assertEqual(len(self.alerts), 1)
        self.assertIn("difference", self.alerts[0])
        self.assertIn("0xdeadbeef", self.alerts[0])

        self.clock.now += telethon_safety.ALERT_INTERVAL_SECONDS
        asyncio.run(fall_back(1))
        self.assertEqual(len(self.alerts), 2)
        self.assertIn("2 more since the last alert", self.alerts[1])

    def test_a_failing_alert_does_not_break_the_fallback(self):
        async def broken_alert(text):
            raise RuntimeError("log chat unreachable")

        self.monitor.alert = broken_alert
        client = self._client(
            {
                functions.updates.GetDifferenceRequest: self._not_found(),
                functions.updates.GetStateRequest: _state(),
            }
        )

        async def fall_back():
            diff = await client(_difference_request())
            await asyncio.sleep(0)
            return diff

        with self.assertLogs(telethon_safety.__name__, logging.WARNING) as logs:
            diff = asyncio.run(fall_back())

        self.assertIsInstance(diff, types.updates.Difference)
        self.assertIn("Could not deliver a safety-net alert", "\n".join(logs.output))


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


class LogCounterTests(_InstalledTestCase):
    def setUp(self):
        logger = logging.getLogger(SENDER_LOGGER)
        previous_level = logger.level
        logger.setLevel(logging.INFO)
        self.addCleanup(logger.setLevel, previous_level)
        self.logger = logger

    def test_counts_telethons_type_not_found_record(self):
        stats = self.install()

        self.logger.info(telethon_safety.TELETHON_TYPE_NOT_FOUND_MSG, 0x1234, b"x")

        self.assertEqual(stats.by_kind[SafetyKind.DROPPED_MESSAGE], 1)
        self.assertEqual(stats.by_constructor[0x1234], 1)

    def test_counts_records_carrying_a_type_not_found_exception(self):
        stats = self.install()
        error = errors.TypeNotFoundError(0x5678, b"")

        self.logger.error(
            "Unhandled error while processing msgs",
            exc_info=(type(error), error, None),
        )
        self.logger.warning("Unrelated")

        self.assertEqual(stats.by_kind[SafetyKind.PROCESSING], 1)
        self.assertEqual(stats.total, 1)
        self.assertEqual(stats.by_constructor[0x5678], 1)


if __name__ == "__main__":
    unittest.main()
