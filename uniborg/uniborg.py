# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.
import asyncio
import importlib.util
import logging
from pathlib import Path
import traceback

from telethon import TelegramClient
import telethon.utils
import telethon.events

from uniborg import util
from .storage import Storage
from . import hacks


class Uniborg(TelegramClient):
    log_chat = -1001179162919 # alicization

    @classmethod
    async def create(
            cls, session, *, plugin_path="plugins", storage=None,
            bot_token=None, **kwargs):
        kwargs = {
            "api_id": 6, "api_hash": "eb06d4abfb49dc3eeb1aeb98ae0f581e",
            **kwargs}
        self = Uniborg(session, **kwargs)
        # TODO: handle non-string session
        #
        # storage should be a callable accepting plugin name -> Storage object.
        # This means that using the Storage type as a storage would work too.
        self._name = session
        self.storage = storage or (lambda n: Storage(Path("data") / n))
        self._logger = logging.getLogger(session)
        self._plugins = {}
        self._plugin_path = plugin_path

        # This is a hack, please avert your eyes
        # We want this in order for the most recently added handler to take
        # precedence
        self._event_builders = hacks.ReverseList()

        await self._async_init(bot_token=bot_token)

        core_plugin = Path(__file__).parent / "_core.py"
        self.load_plugin_from_file(core_plugin)

        for p in Path().glob(f"{self._plugin_path}/*.py"):
            self.load_plugin_from_file(p)
        return self

    async def _async_init(self, **kwargs):
        await self.start(**kwargs)

        self.me = await self.get_me()
        self.uid = telethon.utils.get_peer_id(self.me)
        util.borg = self

    def load_plugin(self, shortname):
        self.load_plugin_from_file(f"{self._plugin_path}/{shortname}.py")

    def load_plugin_from_file(self, path):
        path = Path(path)
        shortname = path.stem # removes extension and dirname
        name = f"_UniborgPlugins.{self._name}.{shortname}"

        spec = importlib.util.spec_from_file_location(name, path)
        mod = importlib.util.module_from_spec(spec)

        mod.borg = self
        mod.logger = logging.getLogger(shortname)
        mod.storage = self.storage(f"{self._name}/{shortname}")

        spec.loader.exec_module(mod)
        self._plugins[shortname] = mod
        self._logger.info(f"Successfully loaded plugin {shortname}")

    def remove_plugin(self, shortname):
        name = self._plugins[shortname].__name__

        for i in reversed(range(len(self._event_builders))):
            ev, cb = self._event_builders[i]
            if cb.__module__ == name:
                del self._event_builders[i]

        del self._plugins[shortname]
        self._logger.info(f"Removed plugin {shortname}")

    async def reload_plugin(self, shortname: str, chat = None):
        chat = chat or self.log_chat
        path = Path(shortname)
        if path.is_file():
            shortname = path.stem
        try:
            if shortname in self._plugins:
                self.remove_plugin(shortname)
            self.load_plugin(shortname)

            await self.send_message(chat,
                f"Successfully (re)loaded plugin {shortname}")
        except Exception as e:
            tb = traceback.format_exc()
            logger.warn(f"Failed to (re)load plugin {shortname}: {tb}")
            await self.send_message(chat, f"Failed to (re)load plugin {shortname}: {e}")

    def await_event(self, event_matcher, filter=None):
        fut = asyncio.Future()

        @self.on(event_matcher)
        async def cb(event):
            try:
                if filter is None or await filter(event):
                    fut.set_result(event)
            except telethon.events.StopPropagation:
                fut.set_result(event)
                raise

        fut.add_done_callback(
            lambda _: self.remove_event_handler(cb, event_matcher))

        return fut
