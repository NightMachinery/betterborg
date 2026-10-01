"""Keeps the test run off the stores under ~/.borg."""

import os
import tempfile

os.environ["borg_media_store_path"] = os.path.join(
    tempfile.mkdtemp(prefix="borg-tests-"), "media_store.sqlite3"
)
