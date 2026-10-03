"""The shell pool and the plugin pool (`util.init_brishes`, behind `.x` and `.xf`).

An old pool is shut down on `util.executor`, which is also the event loop's
default executor, so the cleanup can run long after the restart returned.
"""

import asyncio
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from uniborg import util


class _Lock:
    def release(self):
        pass


class _Pool:
    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.cleaned = False
        self.commands = []

    def cleanup(self):
        self.cleaned = True

    def acquire_lock(self, server_index=None, lock_sleep=1):
        return _Lock(), 0

    def z(self, template, **kwargs):
        pass

    def send_cmd(self, cmd, cmd_stdin="", **kwargs):
        self.commands.append(cmd_stdin)
        return SimpleNamespace(outerr="ok", retcode=0)


class _LateExecutor:
    """Queues submitted work, as a busy executor would, until `run_all`."""

    def __init__(self):
        self.queue = []

    def submit(self, fn, *args):
        self.queue.append((fn, args))

    def run_all(self):
        while self.queue:
            fn, args = self.queue.pop(0)
            fn(*args)


class _PoolTestCase(unittest.TestCase):
    def setUp(self):
        self.executor = _LateExecutor()
        for name, value in (
            ("Brish", _Pool),
            ("executor", self.executor),
            ("persistent_brish", None),
            ("_plugin_brish", None),
        ):
            patcher = patch.object(util, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)


class InitBrishesTests(_PoolTestCase):
    def test_a_late_cleanup_retires_the_old_pool_not_the_new_one(self):
        util.init_brishes()
        old = util.persistent_brish

        util.restart_brishes()
        new = util.persistent_brish
        self.executor.run_all()

        self.assertIsNot(old, new)
        self.assertTrue(old.cleaned)
        self.assertFalse(new.cleaned)

    def test_the_first_pool_has_nothing_to_retire(self):
        util.init_brishes()
        self.executor.run_all()

        self.assertEqual(self.executor.queue, [])
        self.assertFalse(util.persistent_brish.cleaned)


class PluginBrishTests(_PoolTestCase):
    def test_the_plugin_pool_starts_on_first_use_and_is_reused(self):
        util.init_brishes()
        self.assertIsNone(util._plugin_brish)

        pool = util.plugin_brish()

        self.assertIs(util.plugin_brish(), pool)
        self.assertIsNot(pool, util.persistent_brish)
        self.assertEqual(
            pool.kwargs,
            {
                "boot_cmd": util.BRISH_BOOT_CMD,
                "server_count": util.plugin_brish_count,
            },
        )

    def test_a_restart_retires_the_plugin_pool_and_the_next_use_starts_one(self):
        util.init_brishes()
        old = util.plugin_brish()

        util.restart_brishes()
        self.assertIsNone(util._plugin_brish)
        self.executor.run_all()
        new = util.plugin_brish()

        self.assertTrue(old.cleaned)
        self.assertIsNot(new, old)
        self.assertFalse(new.cleaned)

    def test_plugins_default_to_their_pool_and_the_shell_passes_its_own(self):
        util.init_brishes()

        asyncio.run(util.brishz_capture(cwd=None, cmd="printf plugin"))
        asyncio.run(
            util.brishz_capture(
                cwd=None, cmd="printf shell", brish=util.persistent_brish
            )
        )

        self.assertEqual(util.plugin_brish().commands, ["printf plugin"])
        self.assertEqual(util.persistent_brish.commands, ["printf shell"])


if __name__ == "__main__":
    unittest.main()
