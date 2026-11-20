"""Read repository prompts at the point of use so edits take effect on the next request."""

import os
from pathlib import Path


DIRECTORY = Path(__file__).resolve().parents[1] / "prompts"


def read(name: str) -> str:
    """Read a repository Markdown prompt using its extensionless name."""
    return (DIRECTORY / f"{name}.md").read_text(encoding="utf-8")


def legacy(filename: str) -> str:
    """Prefer a repository override and preserve optional operator prompts."""
    try:
        return (DIRECTORY / "legacy" / filename).read_text(encoding="utf-8")
    except FileNotFoundError:
        nightdir = os.environ.get("NIGHTDIR")
        if nightdir:
            try:
                return (Path(nightdir) / "PE" / filename).read_text(encoding="utf-8")
            except FileNotFoundError:
                pass
        return ""
