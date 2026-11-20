"""Telegram formatting for a media profile."""

import re
from urllib.parse import urlsplit

from telethon import errors


SHORTCUT = re.compile(r"^\.(?:media|imdb)(?=\s|$)", re.IGNORECASE)
IMAGE = re.compile(r'^!\[[^\]\n]*\]\((https://[^\s<>]+?)(?: "[^"\n]*")?\)$')
IMAGE_HOSTS = {
    "image.tmdb.org", "m.media-amazon.com", "cdn.myanimelist.net", "images.igdb.com",
    "shared.fastly.steamstatic.com", "shared.akamai.steamstatic.com",
    "cdn.cloudflare.steamstatic.com", "cdn.akamai.steamstatic.com",
    "media.steampowered.com", "cdn.steamstatic.com",
}


def _approved_image(line):
    match = IMAGE.fullmatch(line)
    if match is None:
        return False
    try:
        url = urlsplit(match[1])
        return url.hostname in IMAGE_HOSTS and url.port in (443, None) and url.username is None
    except ValueError:
        return False


def markdown(text, *, images=True):
    """Keep at most two standalone images and pass only Markdown to Telegram."""
    result = []
    image_count = 0
    for line in text.splitlines():
        candidate = line.strip()
        if _approved_image(candidate):
            if images and image_count < 2:
                result.extend(("", candidate, ""))
                image_count += 1
            continue
        # Rich Markdown also accepts raw HTML and inline media. Only the
        # explicit image blocks accepted above should be interpreted as media.
        result.append(line.replace("![", "[").replace("<", "&lt;").replace(">", "&gt;"))
    return "\n".join(result).strip()


async def deliver(send, text):
    """Return True after a rich send, retrying rejected image blocks once."""
    with_images, text_only = markdown(text), markdown(text, images=False)
    for content in dict.fromkeys((with_images, text_only)):
        if not content:
            continue
        try:
            await send(content)
        except (errors.BadRequestError, errors.ForbiddenError, AttributeError, TypeError):
            continue
        return True
    return False


def classic(text):
    """Keep linked ratings legible when a chat only accepts classic Markdown."""
    lines = []
    for line in markdown(text, images=False).splitlines():
        if line.startswith("|"):
            cells = [part.strip() for part in line.strip("|").split("|")]
            if cells[0].lower() == "source" or all(re.fullmatch(r":?-+:?", c) for c in cells):
                continue
            line = "• " + " · ".join(filter(None, cells))
        lines.append(re.sub(r"^#{1,6}\s+(.+)$", r"**\1**", line))
    return "\n".join(lines)
