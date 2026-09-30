"""The bots' Redis credentials (uniborg/redis_auth.py) and the shared client
that uses them (uniborg/redis_util.py).

Terms follow uniborg/redis_auth.py: the admin secret, the bot user, the bot
secret and provisioning. Nothing here talks to a real Redis: `FakeServer`
stands in for one with ACL users, and `FakeClient` for a redis-py client of
it. A restart of the fake server forgets its ACL users, as a real one does
when nothing persisted them, and drops every client's connection.
"""

import asyncio
import contextlib
import hashlib
import importlib
import io
from pathlib import Path
import stat
import sys
import tempfile
import types
import unittest

try:
    from redis.exceptions import (
        AuthenticationError,
        ConnectionError as RedisConnectionError,
        NoPermissionError,
        ResponseError,
    )

    REDIS_IMPORTABLE = True
except ImportError:
    REDIS_IMPORTABLE = False

_ROOT = Path(__file__).resolve().parents[1]


def _load():
    #: Through a stand-in package that has uniborg's directory as its path, so
    #: redis_util's `from . import redis_auth` works while uniborg/__init__.py,
    #: which starts shell servers, never runs.
    name = "_uniborg_redis_under_test"
    if name not in sys.modules:
        package = types.ModuleType(name)
        package.__path__ = [str(_ROOT / "uniborg")]
        sys.modules[name] = package
    return (
        importlib.import_module(f"{name}.redis_auth"),
        importlib.import_module(f"{name}.redis_util"),
    )


if REDIS_IMPORTABLE:
    redis_auth, redis_util = _load()

ADMIN_PASSWORD = "admin-0123456789abcdef"
BOT_URL = "redis://localhost:6379"


def _hash(password):
    return hashlib.sha256(password.encode()).hexdigest()


class FakeServer:
    def __init__(self, *, admin_password=None):
        #: The default user's password; None means Redis asks for none.
        self.admin_password = admin_password
        #: Bot users: name -> (password, rules).
        self.users = {}
        self.setuser_calls = []
        self.clients = []
        #: When set, ACL SETUSER raises `setuser_error(user, password)`.
        self.setuser_error = None
        #: When set, every connection attempt is refused.
        self.down = False
        #: Bumped by `restart`, which drops every open connection.
        self.epoch = 0
        #: Awaited inside every PING, so tests can hold connections open.
        self.ping_gate = None

    def restart(self):
        self.users.clear()
        self.epoch += 1

    def from_url(self, url, **kwargs):
        client = FakeClient(self, url=url, **kwargs)
        self.clients.append(client)
        return client

    def authenticate(self, *, username, password):
        if self.down:
            raise RedisConnectionError("Error 61 connecting to localhost:6379.")
        if username in (None, "default"):
            if self.admin_password is None:
                if password is not None:
                    raise AuthenticationError(
                        "AUTH <password> called without any password configured"
                        " for the default user. Are you sure your configuration"
                        " is correct?"
                    )
            elif password is None:
                raise AuthenticationError("Authentication required.")
            elif password != self.admin_password:
                raise AuthenticationError(
                    "invalid username-password pair or user is disabled."
                )
            return
        user = self.users.get(username)
        if user is None or user[0] != password:
            raise AuthenticationError(
                "invalid username-password pair or user is disabled."
            )

    def acl(self, subcommand, name, *rules):
        if subcommand == "GETUSER":
            user = self.users.get(name)
            if user is None:
                return None
            password, user_rules = user
            return ["passwords", [_hash(password)], "rules", list(user_rules)]
        elif subcommand == "SETUSER":
            self.setuser_calls.append(("ACL", "SETUSER", name, *rules))
            assert rules[0] == "reset" and rules[1].startswith(">"), rules
            password = rules[1][1:]
            if self.setuser_error is not None:
                raise self.setuser_error(name, password)
            self.users[name] = (password, tuple(rules[2:]))
            return "OK"
        else:
            raise ResponseError(f"unknown subcommand {subcommand!r}")


class FakeClient:
    def __init__(
        self, server, *, url, decode_responses=False, username=None, password=None
    ):
        self.server = server
        self.url = url
        self.decode_responses = decode_responses
        self.username = username
        self.password = password
        self.closed = False
        self._epoch = None

    def _ensure_connected(self):
        #: Like redis-py, (re)authenticate whenever there is no live
        #: connection: at first use, and after the server restarted.
        if self._epoch != self.server.epoch:
            self.server.authenticate(username=self.username, password=self.password)
            self._epoch = self.server.epoch

    async def ping(self):
        if self.server.ping_gate is not None:
            await self.server.ping_gate.wait()
        self._ensure_connected()
        return True

    async def execute_command(self, *args):
        self._ensure_connected()
        if args[0] == "ACL":
            if self.username not in (None, "default"):
                raise NoPermissionError(
                    "this user has no permissions to run the 'acl' command"
                )
            return self.server.acl(*args[1:])
        return None

    async def aclose(self):
        self.closed = True


def _run(coro):
    return asyncio.run(coro)


class _Case(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.home = Path(directory.name)
        self.bot_file = self.home / ".borg" / "redis-auth"
        self.admin_file = self.home / ".redis-auth"
        self.logged = []

    def settings(self, **overrides):
        values = dict(
            bot_auth_file=self.bot_file,
            admin_auth_file=self.admin_file,
            admin_secret_env=None,
        )
        values.update(overrides)
        return redis_auth.RedisAuthSettings(**values)

    def connect(self, server, **overrides):
        return _run(
            redis_auth.connect(
                self.settings(**overrides),
                from_url=server.from_url,
                log=self.logged.append,
            )
        )

    def write_bot_secret(self, secret):
        self.bot_file.parent.mkdir(parents=True, exist_ok=True)
        self.bot_file.write_text(secret)


@unittest.skipUnless(REDIS_IMPORTABLE, "redis-py is not installed")
class ConnectTests(_Case):
    def test_rules_are_the_documented_ones_in_order(self):
        self.assertEqual(
            " ".join(redis_auth.BOT_ACL_RULES),
            "on ~borg:* resetchannels -@all +@read +@write +@keyspace"
            " +@transaction +@connection -@dangerous",
        )

    def test_explicit_url_is_used_verbatim_and_nothing_is_provisioned(self):
        #: An open server, since the fake reads no credentials from URLs; the
        #: admin secret is there only to show that nothing is provisioned.
        server = FakeServer()
        url = "redis://elsewhere:6380/2"
        connection = self.connect(
            server, explicit_url=url, admin_secret_env=ADMIN_PASSWORD
        )
        self.assertEqual(connection.source, redis_auth.SOURCE_EXPLICIT_URL)
        self.assertIsNone(connection.warning)
        (client,) = server.clients
        self.assertIs(connection.client, client)
        self.assertEqual(client.url, url)
        self.assertTrue(client.decode_responses)
        self.assertIsNone(client.password)
        self.assertEqual(server.setuser_calls, [])
        self.assertFalse(self.bot_file.exists())

    def test_without_admin_secret_or_bot_file_connects_without_password(self):
        server = FakeServer()
        connection = self.connect(server)
        self.assertEqual(connection.source, redis_auth.SOURCE_NO_PASSWORD)
        self.assertIsNone(connection.warning)
        (client,) = server.clients
        self.assertEqual(client.url, BOT_URL)
        self.assertIsNone(client.username)
        self.assertIsNone(client.password)
        self.assertFalse(self.bot_file.exists())
        self.assertEqual(self.logged, [])

    def test_first_start_creates_the_secret_and_the_user(self):
        server = FakeServer(admin_password=ADMIN_PASSWORD)
        #: From the admin auth file this time, which also covers the newline.
        self.admin_file.write_text(f"{ADMIN_PASSWORD}\n")

        connection = self.connect(server)

        secret = self.bot_file.read_text().strip()
        self.assertEqual(len(secret), 64)
        self.assertEqual(stat.S_IMODE(self.bot_file.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.bot_file.parent.stat().st_mode), 0o700)
        self.assertEqual(
            server.setuser_calls,
            [
                ("ACL", "SETUSER", "borg", "reset", f">{secret}")
                + redis_auth.BOT_ACL_RULES
            ],
        )
        admin_client, bot_client = server.clients
        self.assertEqual(admin_client.password, ADMIN_PASSWORD)
        self.assertTrue(admin_client.closed)
        self.assertIs(connection.client, bot_client)
        self.assertEqual((bot_client.username, bot_client.password), ("borg", secret))
        self.assertFalse(bot_client.closed)
        self.assertEqual(connection.source, redis_auth.bot_user_source("borg"))
        self.assertIsNone(connection.warning)
        self.assertEqual(len(self.logged), 1)
        self.assertIn("'borg'", self.logged[0])
        self.assertNotIn(secret, self.logged[0])

    def test_matching_rules_are_reasserted_without_a_change(self):
        server = FakeServer(admin_password=ADMIN_PASSWORD)
        self.connect(server, admin_secret_env=ADMIN_PASSWORD)
        self.logged.clear()

        connection = self.connect(server, admin_secret_env=ADMIN_PASSWORD)

        self.assertEqual(len(server.setuser_calls), 2)
        self.assertEqual(self.logged, [])
        self.assertEqual(connection.source, redis_auth.bot_user_source("borg"))
        admin = server.from_url(BOT_URL, password=ADMIN_PASSWORD)
        secret = self.bot_file.read_text().strip()
        changed = _run(
            redis_auth.provision_bot_user(admin, user="borg", password=secret)
        )
        self.assertFalse(changed)

    def test_user_lost_in_a_restart_is_provisioned_again(self):
        server = FakeServer(admin_password=ADMIN_PASSWORD)
        first = self.connect(server, admin_secret_env=ADMIN_PASSWORD)
        server.restart()
        with self.assertRaises(AuthenticationError):
            _run(first.client.ping())
        self.logged.clear()

        second = self.connect(server, admin_secret_env=ADMIN_PASSWORD)

        self.assertEqual(len(self.logged), 1)
        self.assertEqual(second.source, redis_auth.bot_user_source("borg"))
        self.assertTrue(_run(second.client.ping()))

    def test_rejected_bot_user_without_admin_secret_raises(self):
        server = FakeServer(admin_password=ADMIN_PASSWORD)
        self.write_bot_secret("f" * 64)
        with self.assertRaises(AuthenticationError):
            self.connect(server)
        (client,) = server.clients
        self.assertTrue(client.closed)

    def test_rejected_bot_user_falls_back_to_the_admin_secret(self):
        server = FakeServer(admin_password=ADMIN_PASSWORD)
        self.write_bot_secret("f" * 64)
        connection = self.connect(
            server, admin_secret_env=ADMIN_PASSWORD, provision=False
        )
        self.assertEqual(connection.source, redis_auth.SOURCE_ADMIN_SECRET)
        self.assertIn("rejected ACL user 'borg'", connection.warning)
        self.assertEqual(server.setuser_calls, [])
        rejected, admin = server.clients
        self.assertTrue(rejected.closed)
        self.assertEqual(admin.password, ADMIN_PASSWORD)

    def test_failed_provisioning_falls_back_without_leaking_secrets(self):
        server = FakeServer(admin_password=ADMIN_PASSWORD)
        server.setuser_error = lambda user, password: ResponseError(
            f"simulated failure quoting {ADMIN_PASSWORD} and {password}"
        )

        connection = self.connect(server, admin_secret_env=ADMIN_PASSWORD)

        secret = self.bot_file.read_text().strip()
        self.assertEqual(connection.source, redis_auth.SOURCE_ADMIN_SECRET)
        self.assertIn("could not provision ACL user 'borg'", connection.warning)
        self.assertIn("simulated failure", connection.warning)
        self.assertIn("admin secret", connection.warning)
        self.assertNotIn(ADMIN_PASSWORD, connection.warning)
        self.assertNotIn(secret, connection.warning)
        *discarded, kept = server.clients
        self.assertTrue(all(client.closed for client in discarded))
        self.assertIs(connection.client, kept)
        self.assertEqual(kept.password, ADMIN_PASSWORD)

    def test_admin_secret_against_an_open_redis_connects_without_password(self):
        #: A host with ~/.redis-auth whose Redis requires no password yet.
        server = FakeServer()
        connection = self.connect(server, admin_secret_env=ADMIN_PASSWORD)
        self.assertEqual(connection.source, redis_auth.SOURCE_NO_PASSWORD)
        self.assertIn("not protected", connection.warning)
        self.assertNotIn(ADMIN_PASSWORD, connection.warning)
        self.assertIsNone(server.clients[-1].password)

    def test_admin_secret_rejected_by_a_hardened_redis_raises(self):
        server = FakeServer(admin_password=ADMIN_PASSWORD)
        with self.assertRaisesRegex(AuthenticationError, "invalid username"):
            self.connect(server, admin_secret_env="wrong", provision=False)

    def test_unreachable_redis_raises_and_closes_its_client(self):
        server = FakeServer()
        server.down = True
        with self.assertRaises(RedisConnectionError):
            self.connect(server)
        self.assertTrue(all(client.closed for client in server.clients))


@unittest.skipUnless(REDIS_IMPORTABLE, "redis-py is not installed")
class BotSecretTests(_Case):
    def test_racing_creators_agree_on_one_secret(self):
        results = []

        def racing_token(nbytes):
            #: The other creator runs to completion between our temp file and
            #: our link, which is the window a real race would hit.
            results.append(
                redis_auth.bot_secret_ensure(self.bot_file, token=lambda n: "b" * 64)
            )
            return "a" * 64

        mine = redis_auth.bot_secret_ensure(self.bot_file, token=racing_token)

        self.assertEqual(results, ["b" * 64])
        self.assertEqual(mine, "b" * 64)
        self.assertEqual(
            sorted(p.name for p in self.bot_file.parent.iterdir()), ["redis-auth"]
        )

    def test_existing_secret_is_returned_unchanged(self):
        self.write_bot_secret("c" * 64 + "\r\n")
        secret = redis_auth.bot_secret_ensure(
            self.bot_file, token=lambda n: self.fail("must not generate")
        )
        self.assertEqual(secret, "c" * 64)

    def test_empty_existing_file_raises_and_is_left_alone(self):
        self.write_bot_secret("")
        with self.assertRaises(redis_auth.RedisAuthError):
            redis_auth.bot_secret_ensure(self.bot_file)
        self.assertEqual(self.bot_file.read_text(), "")
        self.assertEqual(
            [p.name for p in self.bot_file.parent.iterdir()], ["redis-auth"]
        )


@unittest.skipUnless(REDIS_IMPORTABLE, "redis-py is not installed")
class SettingsTests(unittest.TestCase):
    def test_defaults(self):
        settings = redis_auth.settings_from_env({})
        self.assertIsNone(settings.explicit_url)
        self.assertEqual(settings.base_url, redis_auth.DEFAULT_REDIS_URL)
        self.assertEqual(settings.bot_user, "borg")
        self.assertEqual(settings.bot_auth_file, redis_auth.DEFAULT_BOT_AUTH_FILE)
        self.assertIsNone(settings.admin_secret_env)
        self.assertTrue(settings.provision)

    def test_environment_overrides(self):
        settings = redis_auth.settings_from_env(
            {
                "REDIS_URL": "redis://example:1",
                "BORG_REDIS_USER": "borg_test",
                "BORG_REDIS_AUTH_FILE": "~/elsewhere/auth",
                "REDISCLI_AUTH": ADMIN_PASSWORD,
                "BORG_REDIS_PROVISION": " Off ",
            }
        )
        self.assertEqual(settings.explicit_url, "redis://example:1")
        self.assertEqual(settings.bot_user, "borg_test")
        self.assertEqual(settings.bot_auth_file, Path.home() / "elsewhere" / "auth")
        self.assertEqual(settings.admin_secret_env, ADMIN_PASSWORD)
        self.assertFalse(settings.provision)

    def test_provision_flag_values(self):
        for value, expected in [
            ("1", True),
            ("yes", True),
            ("Y", True),
            ("TRUE", True),
            ("on", True),
            ("0", False),
            ("no", False),
            ("n", False),
            ("False", False),
            ("off", False),
            ("", True),
        ]:
            with self.subTest(value=value):
                settings = redis_auth.settings_from_env({"BORG_REDIS_PROVISION": value})
                self.assertIs(settings.provision, expected)

    def test_unknown_provision_value_raises(self):
        with self.assertRaisesRegex(ValueError, "BORG_REDIS_PROVISION"):
            redis_auth.settings_from_env({"BORG_REDIS_PROVISION": "sometimes"})

    def test_repr_hides_secrets(self):
        settings = redis_auth.settings_from_env(
            {"REDIS_URL": "redis://:urlpass@x:1", "REDISCLI_AUTH": ADMIN_PASSWORD}
        )
        self.assertNotIn(ADMIN_PASSWORD, repr(settings))
        self.assertNotIn("urlpass", repr(settings))

    def test_admin_secret_prefers_the_environment(self):
        with tempfile.TemporaryDirectory() as directory:
            admin_file = Path(directory) / ".redis-auth"
            settings = redis_auth.RedisAuthSettings(
                admin_auth_file=admin_file, admin_secret_env=None
            )
            self.assertIsNone(redis_auth.admin_secret(settings))
            admin_file.write_text("")
            self.assertIsNone(redis_auth.admin_secret(settings))
            admin_file.write_text("from-file\n")
            self.assertEqual(redis_auth.admin_secret(settings), "from-file")
            settings = redis_auth.RedisAuthSettings(
                admin_auth_file=admin_file, admin_secret_env="from-env"
            )
            self.assertEqual(redis_auth.admin_secret(settings), "from-env")


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


@unittest.skipUnless(REDIS_IMPORTABLE, "redis-py is not installed")
class SharedClientTests(_Case):
    def setUp(self):
        super().setUp()
        self._reset()
        self.addCleanup(self._reset)
        self.clock = FakeClock()

    @staticmethod
    def _reset():
        redis_util._redis_client = None
        redis_util._last_failure_at = None

    async def get(self, server, **overrides):
        with contextlib.redirect_stdout(io.StringIO()) as out:
            client = await redis_util.get_redis(
                settings=self.settings(**overrides),
                from_url=server.from_url,
                clock=self.clock,
            )
        self.printed = out.getvalue()
        return client

    def test_connects_once_and_reports_the_source(self):
        server = FakeServer(admin_password=ADMIN_PASSWORD)

        async def scenario():
            first = await self.get(server, admin_secret_env=ADMIN_PASSWORD)
            self.assertIn("Connected to Redis via ACL user borg", self.printed)
            second = await self.get(server, admin_secret_env=ADMIN_PASSWORD)
            return first, second

        first, second = _run(scenario())
        self.assertIs(first, second)
        self.assertEqual(first.username, "borg")
        self.assertEqual(len(server.setuser_calls), 1)

    def test_failed_ping_is_not_cached(self):
        server = FakeServer()
        server.down = True
        self.assertIsNone(_run(self.get(server)))
        self.assertIn("Failed to connect to Redis", self.printed)
        self.assertIsNone(redis_util._redis_client)
        (client,) = server.clients
        self.assertTrue(client.closed)

    def test_backoff_skips_retries_inside_the_window_only(self):
        server = FakeServer()
        server.down = True
        self.assertIsNone(_run(self.get(server)))
        self.assertEqual(len(server.clients), 1)

        server.down = False
        self.clock.now += redis_util.RECONNECT_BACKOFF_SECONDS - 1
        self.assertIsNone(_run(self.get(server)))
        self.assertEqual(len(server.clients), 1)

        self.clock.now += 2
        client = _run(self.get(server))
        self.assertIsNotNone(client)
        self.assertEqual(len(server.clients), 2)
        self.assertIsNone(redis_util._last_failure_at)

    def test_concurrent_first_calls_connect_once(self):
        server = FakeServer()

        async def scenario():
            server.ping_gate = asyncio.Event()
            calls = [
                asyncio.ensure_future(
                    redis_util.get_redis(
                        settings=self.settings(),
                        from_url=server.from_url,
                        clock=self.clock,
                    )
                )
                for _ in range(5)
            ]
            await asyncio.sleep(0)
            server.ping_gate.set()
            with contextlib.redirect_stdout(io.StringIO()):
                return await asyncio.gather(*calls)

        clients = _run(scenario())
        self.assertEqual(len(server.clients), 1)
        self.assertTrue(all(client is server.clients[0] for client in clients))

    def test_note_error_drops_the_client_on_authentication_errors_only(self):
        server = FakeServer()

        async def scenario():
            client = await self.get(server)
            for error in [
                ResponseError("WRONGTYPE"),
                RedisConnectionError("reset by peer"),
                NoPermissionError("no permissions"),
                ValueError("unrelated"),
            ]:
                redis_util.note_error(error)
                self.assertIs(redis_util._redis_client, client)
            with contextlib.redirect_stdout(io.StringIO()):
                redis_util.note_error(AuthenticationError("invalid password"))
            self.assertIsNone(redis_util._redis_client)
            await asyncio.sleep(0)
            return client

        client = _run(scenario())
        self.assertTrue(client.closed)

    def test_restart_heals_through_note_error(self):
        server = FakeServer(admin_password=ADMIN_PASSWORD)

        async def scenario():
            first = await self.get(server, admin_secret_env=ADMIN_PASSWORD)
            server.restart()
            try:
                await first.execute_command("GET", "borg:x")
            except AuthenticationError as e:
                with contextlib.redirect_stdout(io.StringIO()):
                    redis_util.note_error(e)
            else:
                self.fail("the forgotten user was accepted")
            second = await self.get(server, admin_secret_env=ADMIN_PASSWORD)
            return first, second

        first, second = _run(scenario())
        self.assertIsNot(first, second)
        self.assertEqual(second.username, "borg")
        self.assertIn("borg", server.users)
        self.assertEqual(len(server.setuser_calls), 2)


if __name__ == "__main__":
    unittest.main()
