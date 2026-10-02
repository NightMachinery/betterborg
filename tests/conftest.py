"""Keeps the test run off the stores under ~/.borg."""

import atexit
import os
import shutil
import tempfile

_store_dir = tempfile.mkdtemp(prefix="borg-tests-")
atexit.register(shutil.rmtree, _store_dir, ignore_errors=True)
os.environ["borg_media_store_path"] = os.path.join(_store_dir, "media_store.sqlite3")
