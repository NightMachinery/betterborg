"""
Common utilities for URL processing and media type detection.
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse


AUDIO_EXTENSIONS = {
    ".mp3",
    ".m4a",
    ".wav",
    ".ogg",
    ".oga",
    ".flac",
    ".aac",
}


@dataclass
class UrlMediaInfo:
    """Information about a URL's media content."""

    mime: Optional[str]
    extension: Optional[str]
    audio_p: bool


def url_extension_get(url: str) -> Optional[str]:
    """
    Extract file extension from URL, handling query parameters correctly.

    Args:
        url: The URL to extract extension from

    Returns:
        File extension (e.g., ".mp3") or None if no extension found

    Examples:
        url_extension_get("http://example.com/audio.mp3") -> ".mp3"
        url_extension_get("http://example.com/audio.mp3?token=xyz") -> ".mp3"
        url_extension_get("http://example.com/path/") -> None
    """
    if not url:
        return None

    try:
        parsed = urlparse(url)
        path = parsed.path

        if not path or path.endswith("/"):
            return None

        suffix = Path(path).suffix.lower()
        return suffix if suffix else None

    except Exception:
        return None


async def url_audio_p(url: str) -> UrlMediaInfo:
    """
    Determine if a URL points to audio content, with fast extension checking.

    First checks if URL ends with known audio extensions to avoid network calls.
    Falls back to fetching mimetype for unknown extensions.

    Args:
        url: The URL to check

    Returns:
        UrlMediaInfo with mime, extension, and audio_p fields
    """
    if not url:
        return UrlMediaInfo(mime=None, extension=None, audio_p=False)

    extension = url_extension_get(url)

    # Fast path: check if extension is a known audio format
    if extension and extension in AUDIO_EXTENSIONS:
        return UrlMediaInfo(
            mime=None,  # Don't fetch mime for known extensions
            extension=extension,
            audio_p=True,
        )

    # Fallback: fetch mimetype for unknown or missing extensions
    from llm_chat_plugins.llm_chat import _check_url_mimetype, get_media_type

    mimetype = await _check_url_mimetype(url)
    is_audio = mimetype and get_media_type(mimetype) == "audio"

    return UrlMediaInfo(mime=mimetype, extension=extension, audio_p=bool(is_audio))
