"""Artwork lookup and Telegram formatting for a media profile."""

import asyncio
from html.parser import HTMLParser
import re
from urllib.parse import urlsplit

import httpx
from telethon import errors


SHORTCUT = re.compile(r"^\.(?:media|imdb)(?=\s|$)", re.IGNORECASE)
IMAGE = re.compile(r'^!\[[^\]\n]*\]\((https://[^\s<>()\[\]"]+)(?: "[^"\n]*")?\)$')
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
RICH_DELIVERY_ERRORS = (errors.BadRequestError, errors.ForbiddenError, AttributeError, TypeError)
TITLE_PAGES = {
    "www.rottentomatoes.com": r"(/(?:m|tv)/[A-Za-z0-9_-]+(?:/s[0-9]{2})?)/?",
    "www.imdb.com": r"(/title/tt[0-9]+)(?:/ratings)?/?",
    "myanimelist.net": r"(/(?:anime|manga)/[0-9]+(?:/[A-Za-z0-9_+-]+)?)/?",
    "store.steampowered.com": r"(/app/[0-9]+/)(?:[A-Za-z0-9_-]+/?)?",
}
ARTWORK_PAGE_LIMIT = 3
ARTWORK_HTML_LIMIT = 256 * 1024
ARTWORK_TIMEOUT = 4


def _approved_image(line):
    match = IMAGE.fullmatch(line)
    if match is None:
        return False
    try:
        url = urlsplit(match[1])
        return url.hostname in IMAGE_HOSTS and url.port in (443, None) and url.username is None
    except ValueError:
        return False


def _title_page(link):
    """Return a public title listing on a known host, without user-supplied parameters."""
    try:
        url = urlsplit(link)
        if url.scheme != "https" or url.username is not None or url.port not in (443, None):
            return None
        path = TITLE_PAGES.get(url.hostname)
        match = re.fullmatch(path, url.path) if path else None
        if match:
            return f"https://{url.hostname}{match[1]}"
    except ValueError:
        pass
    return None


class _ArtworkMetadata(HTMLParser):
    url = None

    def handle_starttag(self, tag, attrs):
        if tag != "meta" or self.url is not None:
            return
        fields = dict(attrs)
        name = fields.get("property") or fields.get("name")
        content = fields.get("content")
        if name in ("og:image", "og:image:secure_url", "twitter:image") and content:
            if _approved_image(f"![Poster]({content})"):
                self.url = content


async def _page_artwork(client, page):
    parser = _ArtworkMetadata()
    remaining = ARTWORK_HTML_LIMIT
    try:
        async with asyncio.timeout(ARTWORK_TIMEOUT), client.stream("GET", page) as response:
            if response.status_code != 200:
                return None
            async for chunk in response.aiter_text(chunk_size=8192):
                parser.feed(chunk[:remaining])
                if parser.url is not None:
                    return parser.url
                remaining -= len(chunk)
                if remaining <= 0:
                    break
    except (httpx.HTTPError, TimeoutError):
        pass
    return None


async def add_artwork(text):
    """Use the artwork published by the exact title pages referenced in a profile."""
    lines = text.strip().splitlines()
    if not lines or not lines[0].startswith("## "):
        return text
    if any(_approved_image(line.strip()) for line in lines):
        return text
    pages = list(dict.fromkeys(
        page for _, link in SOURCE_LINK.findall(text) if (page := _title_page(link))
    ))[:ARTWORK_PAGE_LIMIT]
    if not pages:
        return text
    async with httpx.AsyncClient(follow_redirects=False, trust_env=False) as client:
        artwork = await asyncio.gather(*(_page_artwork(client, page) for page in pages))
    image = next((url for url in artwork if url), None)
    if image is None:
        return text
    return "\n".join((lines[0], "", f"![Poster]({image})", "", *lines[1:]))


def _layout_profile(lines):
    """Keep the title, one line of facts and artwork before the table or credits."""
    if not lines or not lines[0].startswith("## "):
        return lines
    end = next((
        index for index, line in enumerate(lines[1:], 1)
        if line.lstrip().startswith(("|", "- ", "* "))
    ), len(lines))
    intro = lines[1:end]
    details = next((
        line for line in intro if " · " in line and not line.lstrip().startswith("![")
    ), None)
    artwork = next((line for line in intro if line.lstrip().startswith("![")), None)
    if end == len(lines) and not details and not artwork:
        return lines
    blocks = [part for part in (lines[0], details, artwork) if part]
    return ["\n\n".join(blocks), "", *lines[end:]]


def _imdb_first(lines):
    def is_other_source(row):
        return not any(
            page.startswith("https://www.imdb.com/")
            for _, link in SOURCE_LINK.findall(row) if (page := _title_page(link))
        )

    for index, line in enumerate(lines):
        if TABLE_DIVIDER.fullmatch(line.strip()):
            start, end = index + 1, index + 1
            while end < len(lines) and lines[end].lstrip().startswith("|"):
                end += 1
            lines[start:end] = sorted(lines[start:end], key=is_other_source)
            break
    return lines


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
    body = "\n".join(_imdb_first(_layout_profile(result))).strip()
    return "\n\n".join(filter(None, (body, " · ".join(source_links))))


async def deliver(send, text):
    """Return True after a rich send, retrying rejected image blocks once."""
    text = await add_artwork(text)
    with_images, text_only = markdown(text), markdown(text, images=False)
    for content in dict.fromkeys((with_images, text_only)):
        if not content:
            continue
        try:
            await send(content)
        except RICH_DELIVERY_ERRORS:
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
