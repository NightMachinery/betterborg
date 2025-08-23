"""
Common utilities for URL processing and media type detection.
"""

import asyncio
import logging
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

import httpx
from pynight.common_icecream import ic

logger = logging.getLogger(__name__)


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
    mimetype = await _check_url_mimetype(url)
    is_audio = mimetype and get_media_type(mimetype) == "audio"

    return UrlMediaInfo(mime=mimetype, extension=extension, audio_p=bool(is_audio))


def get_media_type(mime_type: str) -> Optional[str]:
    """Determine media type category from MIME type."""
    if not mime_type:
        return None
    if mime_type.startswith("image/"):
        return "image"
    elif mime_type.startswith("audio/"):
        return "audio"
    elif mime_type.startswith("video/"):
        return "video"
    elif mime_type == "application/pdf":
        return "pdf"
    else:
        return None


async def _check_url_mimetype(url: str, *, max_retries: int = 10) -> Optional[str]:
    """
    Determine the mimetype of a URL while minimizing downloads.
    Tries a HEAD request first. Some CDNs (e.g., Acast/CloudFront) return
    misleading content-types (like text/plain) or do not follow redirects for
    HEAD. If the HEAD result looks unhelpful, fallback to a tiny GET using a
    Range request to trigger redirects and read only headers.
    Args:
        url: The URL to check
        max_retries: Maximum number of retry attempts
    Returns:
        The mimetype string if successful, None otherwise
    """
    for attempt in range(max_retries + 1):
        try:
            async with httpx.AsyncClient(
                timeout=5,  # Shorter timeout for better UX
                follow_redirects=True,
                max_redirects=10,  # Limit redirect chains
            ) as client:
                if attempt >= 1:
                    await asyncio.sleep(0.1)
                    # await asyncio.sleep(0.1 * attempt)
                elif False:
                    #: I have completely disabled the head req strategy.
                    ##
                    try:
                        # First attempt: HEAD (fast, no body)
                        head_resp = await client.head(url)
                        if head_resp.status_code == 200:
                            head_ct = (
                                head_resp.headers.get("content-type", "")
                                .split(";")[0]
                                .lower()
                            )
                            if head_ct and head_ct not in ("text/plain", "text/html"):
                                # ic(head_ct)
                                return head_ct
                    except:
                        # Fall through to GET fallback
                        pass
                # Fallback: small GET with Range to force redirects and minimize data
                get_resp = await client.get(url, headers={"Range": "bytes=0-0"})
                if get_resp.status_code in (200, 206):
                    get_ct = (
                        get_resp.headers.get("content-type", "").split(";")[0].lower()
                    )
                    if get_ct:
                        return get_ct
                    else:
                        ic(get_ct, get_resp, get_resp.headers)
                else:
                    ic(get_resp, get_resp.headers, get_resp.status_code)
                    if get_resp.status_code == 403:
                        logger.warning(f"403 Forbidden (not retrying): {url}")
                        return None
        except (httpx.TimeoutException, httpx.RequestError) as e:
            if attempt == max_retries:
                traceback.print_exc()
                logger.warning(
                    f"Network error checking URL {url} after {max_retries + 1} attempts: {e}"
                )
            continue
        except:
            if attempt == max_retries:
                traceback.print_exc()
                logger.error(
                    f"Unexpected error checking URL {url} after {max_retries + 1} attempts"
                )
            continue
        # If we reach here without returning, it means no content type was found but no errors occurred
        # Only break if this is not a network-related issue that should be retried
        # break
    logger.warning(f"Could not determine content type for {url}")
    return None
