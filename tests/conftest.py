"""Keeps the test run off the stores under ~/.borg, and off the user's zsh
startup files."""

import atexit
import os
import shutil
import sys
import tempfile

import pytest

_store_dir = tempfile.mkdtemp(prefix="borg-tests-")
atexit.register(shutil.rmtree, _store_dir, ignore_errors=True)
os.environ["borg_media_store_path"] = os.path.join(_store_dir, "media_store.sqlite3")
#: API keys and the tables built on uniborg.llm_db.
os.environ["borg_llm_db_path"] = os.path.join(_store_dir, "llm_api_keys.db")
#: uniborg.timetracker_util opens its database there when imported.
os.environ["timetracker_dir"] = os.path.join(_store_dir, "timetracker")

#: Every zsh the tests start (`.aa`'s `zsh -c`, the brish workers, which run
#: as zsh scripts) reads `$ZDOTDIR/.zshenv` instead of `~/.zshenv`; an empty
#: directory makes them read no user startup file, so what those files print
#: or start cannot reach a command's output. The bot itself leaves ZDOTDIR
#: alone, so `.aa` and the pools still load the user's files.
_zdotdir = os.path.join(_store_dir, "zdotdir")
os.mkdir(_zdotdir)
os.environ["ZDOTDIR"] = _zdotdir


@pytest.fixture(autouse=True)
def _private_quota_memory():
    """Keeps llm_chat's OpenAI and Codex quota memory out of the local Redis.

    A test that hits a usage limit would otherwise mark the real Codex account
    spent for every bot sharing that Redis.
    """
    plugin = sys.modules.get("llm_chat_plugins.llm_chat")
    if plugin is None or not hasattr(plugin, "OPENAI_QUOTA"):
        yield
        return
    from uniborg import openai_providers

    saved = plugin.OPENAI_QUOTA
    plugin.OPENAI_QUOTA = openai_providers.QuotaMemory()
    try:
        yield
    finally:
        plugin.OPENAI_QUOTA = saved
