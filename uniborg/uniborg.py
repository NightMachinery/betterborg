# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.
import sys
import asyncio
import importlib.util
import logging
from pathlib import Path
import traceback
from icecream import ic

from telethon import TelegramClient
import telethon.utils
import telethon.events

from uniborg import util
from uniborg import llm_db
from uniborg import history_util
from .storage import Storage
from . import hacks
from .util import admins
from .constants import BOT_META_INFO_PREFIX


class Uniborg(TelegramClient):
    # @warn this var can be None in which case send_message will fail and potentially crash the whole program
    log_chat = -1001179162919  # alicization

    @classmethod
    async def create(
        cls,
        session,
        *,
        plugin_path="plugins",
        storage=None,
        bot_token=None,
        log_chat=None,
        **kwargs,
    ):
        kwargs = {"api_id": 6, "api_hash": "eb06d4abfb49dc3eeb1aeb98ae0f581e", **kwargs}
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
        if log_chat:
            try:
                self.log_chat = int(
                    log_chat
                )  # Cannot get entity by phone number as a bot (try using integer IDs, not strings)
            except:
                pass
        try:
            self.log_chat = await self.get_input_entity(self.log_chat)
        except:
            if await self.is_bot():
                print(
                    f"Borg needs a log chat to send some log messages to. Since this is a bot, you need to explicitly set this using the env var 'borg_log_chat', or you won't receive these messages. Trying to set the log chat automatically anyway ..."
                )
                self.log_chat = None
                for admin in admins:
                    try:
                        self.log_chat = await self.get_input_entity(admin)
                    except:
                        # self.log_chat = None
                        pass
            else:
                self.log_chat = await self.get_input_entity("me")

        core_plugin = Path(__file__).parent / "_core.py"
        self.load_plugin_from_file(core_plugin)

        for p in Path().glob(f"{self._plugin_path}/*.py"):
            if p.stem.startswith("."):
                #: these are helper files used by other plugins, not plugins themselves
                #: `watch_plugins` also skips these files
                print(f"Skipping '{p.stem}' due to leading dot")
                continue

            self.load_plugin_from_file(p)
        return self

    async def _async_init(self, **kwargs):
        await self.start(**kwargs)

        self.me = await self.get_me()
        self.uid = telethon.utils.get_peer_id(self.me)

        # Inject borg instance into core modules
        core_modules = [
            util,
            bot_util,
            history_util,
            llm_util,
            tts_util,
            redis_util,
            gemini_live_util,
            llm_db,
        ]
        for module in core_modules:
            module.borg = self

        # Cache bot information for plugin injection
        self._is_bot = await self.is_bot()
        self._bot_id = self.me.id
        self._bot_username = f"@{self.me.username}" if self.me.username else None

    def load_plugin(self, shortname):
        self.load_plugin_from_file(f"{self._plugin_path}/{shortname}.py")

    def load_plugin_from_file(self, path):
        path = Path(path)
        shortname = path.stem  # removes extension and dirname

        if shortname == "timetracker":
            if self.me.bot == False:
                self._logger.info(
                    f"{shortname}: skipped loading (the logged-in user is not a bot)"
                )
                return

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

    async def reload_plugin(self, shortname: str, chat=None):
        chat = chat or self.log_chat
        logger = self._logger
        path = Path(shortname)
        if path.is_file():
            shortname = path.stem
        try:
            if shortname in self._plugins:
                if shortname == "timetracker":
                    # ic(self._plugins["timetracker"])
                    # ic(self._plugins["timetracker"].reload_tt_prepare)

                    await self._plugins["timetracker"].reload_tt_prepare()

                self.remove_plugin(shortname)
            self.load_plugin(shortname)

            await self.send_message(
                chat,
                f"{BOT_META_INFO_PREFIX}# Successfully (re)loaded plugin {shortname}",
            )
        except Exception as e:
            tb = traceback.format_exc()
            logger.warn(f"Failed to (re)load plugin '{shortname}': {tb}")
            if chat:
                await self.send_message(
                    chat,
                    f"{BOT_META_INFO_PREFIX}# Failed to (re)load plugin '{shortname}': {e}",
                )

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

        fut.add_done_callback(lambda _: self.remove_event_handler(cb, event_matcher))

        return fut
