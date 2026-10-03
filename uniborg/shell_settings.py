"""Each admin's settings for the shell (`.a`, `.af`, `.aa`), and its kill switch.

The settings are kept per user in `UserStorage(purpose="shell")`:

- how a running command's output shows (its *preview*), per scope, with the
  scope names and `stream_driver.stream_mode`/`set_stream_mode` the chat
  bot's /stream uses: drafts in private chats, edits in groups, by default;
- what the preview becomes when the command ends (`FinalMode`);
- whether output is shown as a terminal would show it (`render`,
  `term_render`), or raw.

`SHELL_STREAMING`, read once from the environment variable
`borg_shell_streaming`, turns live output off altogether: "0" is off, "1" or
unset is on, and anything else is refused at startup.

This is a core module, so a plugin reload never re-executes it. More is in
docs/shell_streaming.md.
"""

from dataclasses import dataclass, fields
from enum import Enum
import logging
import os
from typing import Any, Optional

from uniborg.storage import UserStorage
from uniborg.stream_driver import StreamMode

_log = logging.getLogger(__name__)

#: The `UserStorage` purpose, so the folder under ~/.borg/.
PURPOSE = "shell"
STREAMING_ENV = "borg_shell_streaming"


def streaming_switch(value: Optional[str]) -> bool:
    """Whether VALUE of `borg_shell_streaming` turns live output on."""
    if value is None or value == "1":
        return True
    elif value == "0":
        return False
    else:
        raise ValueError(f"{STREAMING_ENV} must be 0 or 1, not {value!r}")


#: Off, `.a`, `.af` and `.aa` run as they did before live output: no job, no
#: preview, no renderer.
SHELL_STREAMING = streaming_switch(os.environ.get(STREAMING_ENV))


class FinalMode(str, Enum):
    """What a command that showed a preview sends when it ends."""

    #: The preview becomes the final output.
    EDIT_PREVIEW = "edit_preview"
    #: The final output is a new reply, and the preview goes away.
    NEW_REPLY = "new_reply"


@dataclass
class ShellPrefs:
    #: How a preview shows in private chats, and in groups.
    stream_private: StreamMode = StreamMode.DRAFTS
    stream_groups: StreamMode = StreamMode.EDITS
    final_mode: FinalMode = FinalMode.EDIT_PREVIEW
    #: Show output as a terminal would (`term_render`).
    render: bool = True


_FIELD_TYPES = {f.name: f.type for f in fields(ShellPrefs)}


def _load_value(name: str, value: Any) -> Any:
    kind = _FIELD_TYPES[name]
    if kind is bool:
        if not isinstance(value, bool):
            raise ValueError(f"not a boolean: {value!r}")
        return value
    return kind(value)


class ShellSettings:
    """Reads and writes `ShellPrefs` by user id.

    STORAGE is anything with `get(user_id) -> dict` and `set(user_id, dict)`;
    by default a `UserStorage` for PURPOSE, made on first use. Only values
    that differ from the defaults are stored, so a changed default reaches
    everyone who never set that value. A stored value this version does not
    know is logged and read as its default, and so is stored data that is
    not an object.
    """

    def __init__(self, *, storage: Any = None):
        self._storage = storage

    @property
    def storage(self) -> Any:
        if self._storage is None:
            self._storage = UserStorage(purpose=PURPOSE)
        return self._storage

    def get(self, user_id: int) -> ShellPrefs:
        data = self.storage.get(user_id)
        if data is None:
            data = {}
        elif not isinstance(data, dict):
            #: Valid JSON, but not an object: a hand edit gone wrong.
            _log.warning("Ignoring the shell settings of %s: %r", user_id, data)
            data = {}
        prefs = ShellPrefs()
        for name, value in data.items():
            if name not in _FIELD_TYPES:
                _log.warning("Ignoring the unknown shell setting %r", name)
                continue
            try:
                setattr(prefs, name, _load_value(name, value))
            except ValueError:
                _log.warning("Ignoring the shell setting %s=%r", name, value)
        return prefs

    def set(self, user_id: int, prefs: ShellPrefs) -> bool:
        defaults = ShellPrefs()
        data = {
            f.name: _stored_value(getattr(prefs, f.name))
            for f in fields(prefs)
            if getattr(prefs, f.name) != getattr(defaults, f.name)
        }
        return bool(self.storage.set(user_id, data))


def _stored_value(value: Any) -> Any:
    return value.value if isinstance(value, Enum) else value


#: The settings the shell reads; tests replace it with one on a temp dir.
SETTINGS = ShellSettings()
