"""On/off switches read from environment variables.

One parser for every such variable (`borg_shell_streaming`,
`borg_guest_trigger_guard`, `borg_tg_safety_nets`), so they all accept the
same words. It imports only the standard library.
"""

import os
from typing import Mapping, Optional

ON_VALUES = frozenset({"", "1", "true", "yes", "on"})
OFF_VALUES = frozenset({"0", "false", "no", "off"})


def env_switch(name: str, *, environ: Optional[Mapping[str, str]] = None) -> bool:
    """Reads the switch NAME from ENVIRON (by default `os.environ`).

    Unset or empty is on. 1, true, yes and on are on; 0, false, no and off
    are off; letter case and surrounding blanks do not matter. Anything else
    raises ValueError, so a typo stops the bot rather than guess.
    """
    environ = os.environ if environ is None else environ
    raw = environ.get(name, "")
    value = raw.strip().lower()
    if value in ON_VALUES:
        return True
    elif value in OFF_VALUES:
        return False
    else:
        raise ValueError(
            f"{name}={raw!r} is not a recognised switch; "
            "use 1/true/yes/on or 0/false/no/off"
        )
