"""Shutting the bot down: `.restart` and `.shutdown` (`stdplugins/power_tools.py`).

Nothing here restarts or exits: the plugin's `_restart` and `_quit` are
replaced, and `os.execl` and `sys.exit` fail the test if they are reached.
"""

import asyncio
import importlib.util
import logging
import os
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from uniborg import util

from test_advanced_get_guest import _FakeBorg

PLUGIN_PATH = Path(__file__).resolve().parents[1] / "stdplugins" / "power_tools.py"


def _forbidden(*args, **kwargs):
    raise AssertionError("a test must never restart or exit")


class _Borg(_FakeBorg):
    """Disconnects as Telethon does: every running event handler is cancelled
    and awaited, the one that called `disconnect` included."""

    def __init__(self, calls):
        super().__init__()
        self.calls = calls
        self.handler_tasks = set()

    async def disconnect(self):
        self.calls.append("disconnect")
        for task in self.handler_tasks:
            task.cancel()
        await asyncio.wait(self.handler_tasks)


def _load_power_tools(borg):
    previous = util.borg
    util.borg = borg
    try:
        spec = importlib.util.spec_from_file_location("_test_power_tools", PLUGIN_PATH)
        mod = importlib.util.module_from_spec(spec)
        mod.borg = borg
        mod.logger = logging.getLogger("test.power_tools")
        spec.loader.exec_module(mod)
        return mod
    finally:
        util.borg = previous


class PowerToolsTests(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self.borg = _Borg(self.calls)
        self.plugin = _load_power_tools(self.borg)
        for target, name, value in (
            (os, "execl", _forbidden),
            (sys, "exit", _forbidden),
            (self.plugin, "_restart", lambda: self.calls.append("restart")),
            (self.plugin, "_quit", lambda: self.calls.append("quit")),
        ):
            patcher = patch.object(target, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def handler(self, name):
        (handler,) = [fn for _b, fn in self.borg.handlers if fn.__name__ == name]
        return handler

    def run_handler(self, name):
        """Runs handler NAME as Telethon would, as an event handler's task."""
        event = SimpleNamespace(reply=AsyncMock(), edit=AsyncMock())

        async def main():
            task = asyncio.ensure_future(self.handler(name)(event))
            self.borg.handler_tasks.add(task)
            await asyncio.wait({task})
            await asyncio.wait_for(asyncio.gather(*self.plugin._TASKS), 10)

        asyncio.run(main())
        return event

    def test_restart_replies_then_restarts_after_the_disconnect(self):
        event = self.run_handler("restart_handler")

        event.reply.assert_awaited_once_with("Restarted.")
        self.assertEqual(self.calls, ["disconnect", "restart"])

    def test_shutdown_quits_after_the_disconnect(self):
        event = self.run_handler("shutdown_handler")

        event.edit.assert_awaited_once_with("Turning off ...")
        self.assertEqual(self.calls, ["disconnect", "quit"])


if __name__ == "__main__":
    unittest.main()
