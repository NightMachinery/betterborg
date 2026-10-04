"""Keeps the test run off the stores under ~/.borg, and off the user's zsh
startup files."""

import atexit
import os
import shutil
import tempfile

_store_dir = tempfile.mkdtemp(prefix="borg-tests-")
atexit.register(shutil.rmtree, _store_dir, ignore_errors=True)
os.environ["borg_media_store_path"] = os.path.join(_store_dir, "media_store.sqlite3")

#: Every zsh the tests start (`.aa`'s `zsh -c`, the brish workers, which run
#: as zsh scripts) reads `$ZDOTDIR/.zshenv` instead of `~/.zshenv`; an empty
#: directory makes them read no user startup file, so what those files print
#: or start cannot reach a command's output. The bot itself leaves ZDOTDIR
#: alone, so `.aa` and the pools still load the user's files.
_zdotdir = os.path.join(_store_dir, "zdotdir")
os.mkdir(_zdotdir)
os.environ["ZDOTDIR"] = _zdotdir
