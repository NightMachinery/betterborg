"""Restarting the shell pool (`util.init_brishes`, behind `.x` and `.xf`).

The old pool is shut down on `util.executor`, which is also the event loop's
default executor, so the cleanup can run long after the restart returned.
"""

import unittest
from unittest.mock import patch

from uniborg import util


class _Pool:
    def __init__(self, **kwargs):
        self.cleaned = False

    def cleanup(self):
        self.cleaned = True


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


class InitBrishesTests(unittest.TestCase):
    def setUp(self):
        self.executor = _LateExecutor()
        for name, value in (
            ("Brish", _Pool),
            ("executor", self.executor),
            ("persistent_brish", None),
        ):
            patcher = patch.object(util, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

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


if __name__ == "__main__":
    unittest.main()
