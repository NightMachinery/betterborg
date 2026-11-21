"""Telegram formatting for a media profile."""

import re
from urllib.parse import urlsplit

from telethon import errors


SHORTCUT = re.compile(r"^\.(?:media|imdb)(?=\s|$)", re.IGNORECASE)
IMAGE = re.compile(r'^!\[[^\]\n]*\]\((https://[^\s<>]+?)(?: "[^"\n]*")?\)$')
IMAGE_HOSTS = {
    "image.tmdb.org", "m.media-amazon.com", "cdn.myanimelist.net", "images.igdb.com",
    "resizing.flixster.com",
    "shared.fastly.steamstatic.com", "shared.akamai.steamstatic.com",
    "cdn.cloudflare.steamstatic.com", "cdn.akamai.steamstatic.com",
    "media.steampowered.com", "cdn.steamstatic.com",
}
SOURCE_LINK = re.compile(r"\[([^\[\]\n]+)\]\((https://(?:[^\s<>()]|\([^\s<>()]*\))+)\)")
PARENTHESIZED_SOURCE = re.compile(r"[ \t]*\(" + SOURCE_LINK.pattern + r"\)")
TABLE_DIVIDER = re.compile(r"\|(?:\s*:?-+:?\s*\|)+")
MAX_SOURCES = 3


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
    """Keep one approved image and move prose references to a short footer."""
    result = []
    has_image = False
    linked_urls = set()
    footer_sources = {}
    inline_sources = {}

    def collect(match, sources):
        label, url = match.groups()
        sources.setdefault(url, f"[{label}]({url})")
        return label

    def remove_citation(match):
        collect(match, inline_sources)
        return ""

    for line in text.splitlines():
        candidate = line.strip()
        if candidate.startswith("!["):
            if images and not has_image and _approved_image(candidate):
                result.extend(("", candidate, ""))
                has_image = True
            continue
        # Rich Markdown can also embed media using raw HTML and inline syntax.
        line = line.replace("![", "[").replace("<", "&lt;").replace(">", "&gt;")
        if candidate.startswith("|"):
            linked_urls.update(url for _, url in SOURCE_LINK.findall(line))
        elif SOURCE_LINK.search(line):
            if not SOURCE_LINK.sub("", line).strip(" \t·"):
                for match in SOURCE_LINK.finditer(line):
                    collect(match, footer_sources)
                continue
            line = PARENTHESIZED_SOURCE.sub(remove_citation, line)
            line = SOURCE_LINK.sub(lambda match: collect(match, inline_sources), line)
        result.append(line)

    source_links = []
    for url, link in (*footer_sources.items(), *inline_sources.items()):
        if url not in linked_urls:
            source_links.append(link)
            linked_urls.add(url)
        if len(source_links) == MAX_SOURCES:
            break
    body = "\n".join(result).strip()
    return "\n\n".join(filter(None, (body, " · ".join(source_links))))


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
    rich_lines = markdown(text, images=False).splitlines()
    for line, next_line in zip(rich_lines, rich_lines[1:] + [""]):
        row = line.strip()
        if row.startswith("|"):
            if TABLE_DIVIDER.fullmatch(row) or TABLE_DIVIDER.fullmatch(next_line.strip()):
                continue
            cells = [part.strip() for part in row.strip("|").split("|")]
            line = "• " + " · ".join(filter(None, cells))
        lines.append(re.sub(r"^#{1,6}\s+(.+)$", r"**\1**", line))
    return "\n".join(lines)
