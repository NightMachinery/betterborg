# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.

import json
import os
from pathlib import Path

# Note: UserStorage requires the 'filelock' library.
# Install it with: pip install filelock
from filelock import FileLock, Timeout


# ---------------------------------------------------------------------------
# Original Single-File Storage Class
# ---------------------------------------------------------------------------

FILE_NAME = "data.json"


class Storage:
    """
    A simple storage class that saves all its data into a single `data.json`
    file. Best suited for simple, single-process applications without
    concurrent write needs.
    """

    class _Guard:
        def __init__(self, storage):
            self._storage = storage

        def __enter__(self):
            self._storage._autosave = False

        def __exit__(self, *args):
            self._storage._autosave = True
            self._storage._save()

    def __init__(self, root):
        self._root = Path(root)
        self._autosave = True
        self._guard = self._Guard(self)
        if (self._root / FILE_NAME).is_file():
            try:
                with open(self._root / FILE_NAME) as fp:
                    self._data = json.load(fp)
            except json.JSONDecodeError:
                print(
                    f"Warning: {self._root / FILE_NAME} is corrupted. Initializing with empty data."
                )

                self._data = {}
        else:
            self._data = {}

    def bulk_save(self):
        return self._guard

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError("You can only access existing private members")
        return self._data.get(name, None)

    def __setattr__(self, name, value):
        if name.startswith("_"):
            self.__dict__[name] = value
        else:
            self._data[name] = value
            if self._autosave:
                self._save()

    def _save(self):
        if not self._root.is_dir():
            self._root.mkdir(parents=True, exist_ok=True)
        with open(self._root / FILE_NAME, "w") as fp:
            json.dump(self._data, fp)


# ---------------------------------------------------------------------------
# New Per-User Storage Class with File Locking
# ---------------------------------------------------------------------------


class UserStorage:
    """
    Manages storing and retrieving user-specific data in individual JSON files
    with process-safe file locking. Ideal for multi-user bots or applications
    with potential for concurrent access.
    """

    def __init__(self, purpose: str):
        """
        Initializes the storage for a specific purpose (e.g., 'llm_chat').
        This purpose will be used as the subdirectory name under ~/.borg/
        """
        if not purpose or not isinstance(purpose, str):
            raise ValueError(
                "Purpose must be a valid string for the subdirectory name."
            )
        self.base_dir = Path(os.path.expanduser("~/.borg/")) / purpose
        self.base_dir.mkdir(parents=True, exist_ok=True)

    def _get_user_paths(self, user_id: int) -> tuple[Path, Path]:
        """Returns the data file path and the lock file path for a user."""
        file_path = self.base_dir / f"{user_id}.json"
        lock_path = self.base_dir / f"{user_id}.json.lock"
        return file_path, lock_path

    def get(self, user_id: int) -> dict:
        """
        Retrieves a user's data as a dictionary.
        Returns an empty dictionary if the file doesn't exist, is corrupt,
        or if a lock cannot be acquired in time.
        """
        file_path, lock_path = self._get_user_paths(user_id)
        if not file_path.exists():
            return {}

        lock = FileLock(lock_path, timeout=5)
        try:
            with lock:
                with open(file_path, "r", encoding="utf-8") as f:
                    return json.load(f)
        except (json.JSONDecodeError, Timeout) as e:
            print(
                f"Warning: Could not read data for user {user_id}. Returning default. Error: {e}"
            )
            return {}
        except Exception as e:
            print(
                f"Warning: An unexpected error occurred reading data for user {user_id}. Error: {e}"
            )
            return {}

    def set(self, user_id: int, data: dict) -> bool:
        """
        Atomically saves a user's data from a dictionary to their JSON file.
        """
        if not isinstance(data, dict):
            raise TypeError("Data must be a dictionary.")

        file_path, lock_path = self._get_user_paths(user_id)
        lock = FileLock(lock_path, timeout=5)
        try:
            with lock:
                with open(file_path, "w", encoding="utf-8") as f:
                    json.dump(data, f, indent=4)

            return True

        except Timeout:
            # In a bot context, it's often better to log and fail silently
            # than to crash the handler.
            print(
                f"Error: Could not acquire lock to save data for user {user_id}. Write operation skipped."
            )

            return False

        except Exception as e:
            print(
                f"Error: An unexpected error occurred while saving data for user {user_id}: {e}"
            )


################
