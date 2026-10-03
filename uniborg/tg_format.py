"""Text helpers for Telegram's formats: UTF-16 lengths and rich-message flattening.

Telegram counts message lengths and entity offsets in UTF-16 code units, so a
character outside the Basic Multilingual Plane (most emoji) costs two.
`utf16_len`, `truncate_utf16` and `tail_utf16` measure and cut text by that
count.

A *rich message* (MTProto layer 227 and later) carries no text: the server
parses the Markdown or HTML it was sent into a tree of Instant View
``PageBlock*`` blocks holding ``Text*`` rich-text nodes, and returns only that
tree in ``Message.rich_message`` (``message.message`` is ``''``). The source is
never returned. `flatten_rich_message` turns the tree back into GitHub-flavoured
Markdown for history, quoting, trigger detection and export.

This module imports only the standard library. It never imports
``uniborg.util`` (which starts Brish servers) and never imports Telethon types:
the flattener dispatches on class names, so it loads and runs on Telethon
1.43.2 (layer 224), where the rich types do not exist, as well as on 1.45.0.
"""

from dataclasses import dataclass
import datetime
import html
import re
from typing import Callable, Iterable, Optional


def utf16_len(text: str) -> int:
    """Return the length of TEXT in UTF-16 code units, as Telegram counts it."""
    return len(text.encode("utf-16-le")) // 2


def truncate_utf16(text: str, max_units: int, *, suffix: str = "…") -> str:
    """Cut TEXT to at most MAX_UNITS UTF-16 code units, ending with SUFFIX.

    TEXT that already fits is returned unchanged. Otherwise whole characters
    are kept while they and SUFFIX still fit, so a surrogate pair is never
    split. When SUFFIX alone is longer than MAX_UNITS, the result is SUFFIX.
    """
    if utf16_len(text) <= max_units:
        return text
    budget = max_units - utf16_len(suffix)
    kept = []
    used = 0
    for character in text:
        units = utf16_len(character)
        if used + units > budget:
            break
        kept.append(character)
        used += units
    return "".join(kept) + suffix


def tail_utf16(text: str, max_units: int) -> str:
    """The end of TEXT that fits MAX_UNITS UTF-16 code units.

    Whole characters are kept from the end while they fit, so a surrogate pair
    is never split.
    """
    kept = []
    used = 0
    for character in reversed(text):
        units = utf16_len(character)
        if used + units > max_units:
            break
        kept.append(character)
        used += units
    return "".join(reversed(kept))


def truncate_utf8(text: str, max_bytes: int, *, suffix: str = "…") -> str:
    """Cut TEXT to at most MAX_BYTES bytes of UTF-8, ending with SUFFIX.

    For limits counted in UTF-8, such as a rich message's. TEXT that already
    fits is returned unchanged; otherwise no character is split.
    """
    data = text.encode("utf-8")
    if len(data) <= max_bytes:
        return text
    kept = data[: max(0, max_bytes - len(suffix.encode("utf-8")))]
    return kept.decode("utf-8", errors="ignore") + suffix


##
#: How an unknown constructor is shown in the flattened text.
UNSUPPORTED_MARKER = "[unsupported: {}]"

#: Reported in place of a type name when the tree nests deeper than `max_depth`.
NESTING_TOO_DEEP = "NestingTooDeep"

#: Telegram allows 16 levels of nested formatting and blocks, and each level
#: costs a frame or two here, so this leaves room without risking RecursionError.
DEFAULT_MAX_DEPTH = 64

#: Telegram allows 20 columns. The clamp keeps a hostile colspan from
#: allocating a huge grid.
_MAX_TABLE_COLUMNS = 64

#: The rich text editor pads empty paragraphs with a lone U+200B. Only that
#: exact paragraph is dropped: BOT_META_INFO_PREFIX (four U+200B) marks this
#: repo's meta messages and must survive.
_EDITOR_PADDING = "\u200b"

_FOOTNOTE_NAME_RE = re.compile(r"[^\s\[\]^]+")
_FOOTNOTE_REF_RE = re.compile(r"\[\^[^\s\[\]^]+\]")
_BACKTICK_RUN_RE = re.compile(r"`+")
#: A pipe, with the backslashes before it doubled so they stay literal.
_CELL_PIPE_RE = re.compile(r"(\\*)\|")

UnsupportedFn = Callable[[str], None]


@dataclass(frozen=True)
class FlattenedRichMessage:
    """A rich message rendered as Markdown, plus what the rendering could not do."""

    text: str
    #: The server sent only the start of a long message.
    #: ``messages.getRichMessage`` returns the whole one.
    part: bool
    #: Constructor names rendered as `UNSUPPORTED_MARKER`, in first-seen order.
    unsupported: tuple[str, ...]

    @classmethod
    def from_rich(
        cls,
        rich,
        *,
        on_unsupported: Optional[UnsupportedFn] = None,
        max_depth: int = DEFAULT_MAX_DEPTH,
    ) -> "FlattenedRichMessage":
        """Flatten RICH (a ``RichMessage``, an Instant View ``Page`` or None).

        Unknown block and text constructors never raise. Each renders as
        ``[unsupported: <TypeName>]``, and ON_UNSUPPORTED is called once per
        distinct name after rendering. This deliberately departs from the
        repo's raise-on-unknown rule for enum-like values: the tree is
        untrusted server input, every new layer adds constructors, and one new
        block must not make a whole message unreadable.
        """
        if rich is None:
            return cls(text="", part=False, unsupported=())
        blocks = list(getattr(rich, "blocks", None) or [])
        documents = _index_documents(getattr(rich, "documents", None))
        renderer = _Renderer(documents=documents, max_depth=max_depth)
        text = renderer.blocks(blocks, depth=0)
        footnotes = frozenset(renderer.leading_references) & renderer.linked_anchors
        if footnotes:
            #: Footnote links can precede their definitions, so the first pass
            #: finds the footnotes and the second renders them.
            renderer = _Renderer(
                documents=documents, max_depth=max_depth, footnotes=footnotes
            )
            text = renderer.blocks(blocks, depth=0)
        unsupported = tuple(renderer.unsupported)
        if on_unsupported is not None:
            for name in unsupported:
                on_unsupported(name)
        return cls(
            text=text,
            part=bool(getattr(rich, "part", False)),
            unsupported=unsupported,
        )


def flatten_rich_message(
    rich,
    *,
    on_unsupported: Optional[UnsupportedFn] = None,
    max_depth: int = DEFAULT_MAX_DEPTH,
) -> str:
    """Return RICH as GitHub-flavoured Markdown.

    Use `FlattenedRichMessage.from_rich` when the caller also needs ``part``
    (to fetch the full message) or the unsupported constructor names.

    Plain text is not Markdown-escaped. The output is read by people, LLMs and
    command matchers, for which ``\\_`` in ``/some_command`` would be noise, and
    the original source cannot be recovered exactly anyway. Table cells are
    the exception: ``|`` is escaped there, because it would split the cell.
    """
    return FlattenedRichMessage.from_rich(
        rich, on_unsupported=on_unsupported, max_depth=max_depth
    ).text


##
def _type_name(obj) -> str:
    return type(obj).__name__


def _index_documents(documents) -> dict:
    index = {}
    for document in documents or []:
        document_id = getattr(document, "id", None)
        if document_id is not None:
            index[document_id] = document
    return index


def _prefix_lines(text: str, *, first: str, rest: str) -> str:
    lines = text.split("\n")
    out = [first + lines[0] if lines[0] else first.rstrip()]
    out.extend(rest + line if line else rest.rstrip() for line in lines[1:])
    return "\n".join(out)


def _quote(text: str) -> str:
    return _prefix_lines(text, first="> ", rest="> ")


def _wrap(inner: str, *, left: str, right: str) -> str:
    """Wrap INNER in delimiters, keeping its edge whitespace outside them.

    CommonMark does not open ``**`` before a space or close it after one.
    """
    core = inner.strip()
    if not core:
        return inner
    lead = inner[: len(inner) - len(inner.lstrip())]
    trail = inner[len(inner.rstrip()) :]
    return f"{lead}{left}{core}{right}{trail}"


def _code_fence(code: str, *, minimum: int) -> str:
    longest = max((len(run) for run in _BACKTICK_RUN_RE.findall(code)), default=0)
    return "`" * max(minimum, longest + 1)


def _one_line(text: str) -> str:
    return " ".join(text.split("\n"))


def _link_label(text: str) -> str:
    return text.replace("[", "\\[").replace("]", "\\]")


def _link_target(url: str) -> str:
    return url.replace(" ", "%20").replace("(", "%28").replace(")", "%29")


def _placeholder(kind: str, *, detail: str = "", caption: str = "") -> str:
    label = f"{kind} {detail}" if detail else kind
    return f"[{label}: {caption}]" if caption else f"[{label}]"


def _html_attr(value: str) -> str:
    return html.escape(value, quote=True)


def _format_date(value) -> str:
    if isinstance(value, datetime.datetime):
        return value.date().isoformat()
    return ""


def _format_coordinate(value) -> str:
    if isinstance(value, (int, float)):
        return f"{value:.6g}"
    return ""


class _Renderer:
    """One flattening pass over a block tree."""

    def __init__(
        self,
        *,
        documents: dict,
        max_depth: int,
        footnotes: frozenset = frozenset(),
    ):
        self.documents = documents
        self.max_depth = max_depth
        #: Reference anchors rendered as ``[^name]: text``, and linked as ``[^name]``.
        self.footnotes = footnotes
        #: Reference anchors seen at the start of a block, and the anchor names
        #: in-document links point to. A footnote needs both.
        self.leading_references: list[str] = []
        self.linked_anchors: set[str] = set()
        self.unsupported: list[str] = []

    def unsupported_marker(self, name: str) -> str:
        if name not in self.unsupported:
            self.unsupported.append(name)
        return UNSUPPORTED_MARKER.format(name)

    def too_deep(self, depth: int) -> bool:
        return depth > self.max_depth

    ## Blocks
    def blocks(self, blocks: Iterable, *, depth: int) -> str:
        parts = (self.block(block, depth=depth) for block in blocks or [])
        return "\n\n".join(part for part in parts if part)

    def block(self, block, *, depth: int) -> str:
        if block is None:
            return ""
        if self.too_deep(depth):
            return self.unsupported_marker(NESTING_TOO_DEEP)
        handler = _BLOCK_RENDERERS.get(_type_name(block))
        if handler is None:
            return self.unsupported_marker(_type_name(block))
        return handler(self, block, depth + 1)

    def _paragraph(self, block, depth: int) -> str:
        text = self.inline(getattr(block, "text", None), depth=depth, leading=True)
        if text == _EDITOR_PADDING:
            return ""
        return text

    def _heading(self, block, depth: int) -> str:
        text = _one_line(self.inline(getattr(block, "text", None), depth=depth))
        if not text.strip():
            return ""
        return "#" * _HEADING_LEVELS[_type_name(block)] + " " + text

    def _author_date(self, block, depth: int) -> str:
        author = self.inline(getattr(block, "author", None), depth=depth)
        date = _format_date(getattr(block, "published_date", None))
        return ", ".join(part for part in (author, date) if part)

    def _preformatted(self, block, depth: int) -> str:
        code = self.plain(getattr(block, "text", None), depth=depth).rstrip("\n")
        language = _one_line(getattr(block, "language", None) or "").strip()
        fence = _code_fence(code, minimum=3)
        return f"{fence}{language}\n{code}\n{fence}"

    def _math_block(self, block, depth: int) -> str:
        source = (getattr(block, "source", None) or "").strip("\n")
        return f"$$\n{source}\n$$"

    def _divider(self, block, depth: int) -> str:
        return "---"

    def _anchor_block(self, block, depth: int) -> str:
        return f'<a name="{_html_attr(getattr(block, "name", None) or "")}"></a>'

    def _unsupported_block(self, block, depth: int) -> str:
        return self.unsupported_marker(_type_name(block))

    def _blockquote(self, block, depth: int) -> str:
        body = self.inline(getattr(block, "text", None), depth=depth)
        return self._quote_with_caption(
            body, caption=getattr(block, "caption", None), depth=depth
        )

    def _blockquote_blocks(self, block, depth: int) -> str:
        body = self.blocks(getattr(block, "blocks", None), depth=depth)
        return self._quote_with_caption(
            body, caption=getattr(block, "caption", None), depth=depth
        )

    def _quote_with_caption(self, body: str, *, caption, depth: int) -> str:
        cite = _one_line(self.inline(caption, depth=depth))
        if cite:
            body = f"{body}\n\n<cite>{cite}</cite>" if body else f"<cite>{cite}</cite>"
        return _quote(body) if body else ""

    def _details(self, block, depth: int) -> str:
        title = _one_line(self.inline(getattr(block, "title", None), depth=depth))
        body = self.blocks(getattr(block, "blocks", None), depth=depth)
        return self._details_markup(
            title=title, body=body, is_open=bool(getattr(block, "open", False))
        )

    def _thinking(self, block, depth: int) -> str:
        body = self.inline(getattr(block, "text", None), depth=depth)
        return self._details_markup(title="Thinking", body=body, is_open=False)

    def _details_markup(self, *, title: str, body: str, is_open: bool) -> str:
        head = "<details open>" if is_open else "<details>"
        parts = [f"{head}<summary>{title}</summary>"]
        if body:
            parts.append(body)
        parts.append("</details>")
        return "\n\n".join(parts)

    def _list(self, block, depth: int) -> str:
        items = list(getattr(block, "items", None) or [])
        lines = [self._list_item(item, marker="-", depth=depth) for item in items]
        return "\n".join(lines)

    def _ordered_list(self, block, depth: int) -> str:
        items = list(getattr(block, "items", None) or [])
        descending = bool(getattr(block, "reversed", False))
        start = getattr(block, "start", None)
        if not isinstance(start, int):
            start = len(items) if descending else 1
        step = -1 if descending else 1
        number = start
        lines = []
        for item in items:
            number = _explicit_item_number(item, fallback=number)
            lines.append(
                self._list_item(item, marker=f"{max(number, 0)}.", depth=depth)
            )
            number += step
        return "\n".join(lines)

    def _list_item(self, item, *, marker: str, depth: int) -> str:
        name = _type_name(item)
        if name in _TEXT_LIST_ITEMS:
            body = self.inline(getattr(item, "text", None), depth=depth, leading=True)
        elif name in _BLOCK_LIST_ITEMS:
            body = self._item_blocks(getattr(item, "blocks", None), depth=depth)
        else:
            body = self.unsupported_marker(name)
        box = ""
        if getattr(item, "checkbox", False):
            box = "[x] " if getattr(item, "checked", False) else "[ ] "
        return _prefix_lines(
            body, first=f"{marker} {box}", rest=" " * (len(marker) + 1)
        )

    def _item_blocks(self, blocks, *, depth: int) -> str:
        #: A nested list directly under the item's text keeps the list tight.
        out = ""
        for block in blocks or []:
            part = self.block(block, depth=depth)
            if not part:
                continue
            if not out:
                out = part
            elif _type_name(block) in _LIST_BLOCKS:
                out = f"{out}\n{part}"
            else:
                out = f"{out}\n\n{part}"
        return out

    def _table(self, block, depth: int) -> str:
        title = _one_line(self.inline(getattr(block, "title", None), depth=depth))
        grid = self._table_grid(list(getattr(block, "rows", None) or []), depth)
        table = _gfm_table(grid)
        return "\n\n".join(part for part in (title, table) if part)

    def _table_grid(self, rows: list, depth: int) -> list:
        """Lay RICH table rows out on a grid, as a list of rows of `_GridCell`.

        GFM has no colspan or rowspan, so a spanning cell keeps its text in
        its first slot and leaves the slots it covers empty.
        """
        covered = set()
        grid = []
        for row_index, row in enumerate(rows):
            placed = {}
            if _type_name(row) == "PageTableRow":
                cells = list(getattr(row, "cells", None) or [])
            else:
                cells = [row]
            column = 0
            for cell in cells:
                while (row_index, column) in covered:
                    column += 1
                if column >= _MAX_TABLE_COLUMNS:
                    break
                placed[column] = self._grid_cell(cell, depth)
                colspan = _span(getattr(cell, "colspan", None))
                rowspan = _span(getattr(cell, "rowspan", None))
                colspan = min(colspan, _MAX_TABLE_COLUMNS - column)
                rowspan = min(rowspan, len(rows) - row_index)
                for down in range(rowspan):
                    for across in range(colspan):
                        if down or across:
                            covered.add((row_index + down, column + across))
                column += colspan
            width = max(
                [column + 1 for column in placed]
                + [c + 1 for (r, c) in covered if r == row_index],
                default=0,
            )
            grid.append(
                [placed.get(column, _GridCell.filler()) for column in range(width)]
            )
        return grid

    def _grid_cell(self, cell, depth: int) -> "_GridCell":
        if _type_name(cell) != "PageTableCell":
            return _GridCell(
                text=self.unsupported_marker(_type_name(cell)),
                header=False,
                align="",
            )
        text = self.inline(getattr(cell, "text", None), depth=depth)
        text = _CELL_PIPE_RE.sub(
            lambda match: match.group(1) * 2 + "\\|",
            text.replace("\r\n", "\n").replace("\n", "<br>"),
        )
        align = ""
        if getattr(cell, "align_center", False):
            align = "center"
        elif getattr(cell, "align_right", False):
            align = "right"
        return _GridCell(
            text=text, header=bool(getattr(cell, "header", False)), align=align
        )

    def _caption(self, caption, depth: int) -> str:
        if caption is None:
            return ""
        text = _one_line(self.inline(getattr(caption, "text", None), depth=depth))
        credit = _one_line(self.inline(getattr(caption, "credit", None), depth=depth))
        if text and credit:
            return f"{text} ({credit})"
        return text or credit

    def _media_label(self, document_id, *, default: str) -> "_MediaLabel":
        """Name a media block's attachment from its document's attributes."""
        document = self.documents.get(document_id)
        kind = default
        file_name = ""
        for attribute in getattr(document, "attributes", None) or []:
            attribute_name = _type_name(attribute)
            if attribute_name == "DocumentAttributeFilename":
                file_name = getattr(attribute, "file_name", None) or ""
            elif attribute_name == "DocumentAttributeAudio" and getattr(
                attribute, "voice", False
            ):
                kind = "voice note"
            elif attribute_name == "DocumentAttributeAnimated":
                kind = "animation"
        return _MediaLabel(kind=kind, detail=file_name)

    def _media(self, block, depth: int) -> str:
        name = _type_name(block)
        caption = self._caption(getattr(block, "caption", None), depth)
        spec = _MEDIA_KINDS[name]
        label = _MediaLabel(kind=spec.kind, detail="")
        if spec.id_field:
            label = self._media_label(
                getattr(block, spec.id_field, None), default=spec.kind
            )
        return _placeholder(label.kind, detail=label.detail, caption=caption)

    def _embed(self, block, depth: int) -> str:
        caption = self._caption(getattr(block, "caption", None), depth)
        url = getattr(block, "url", None) or ""
        return _placeholder("embed", detail=url, caption=caption)

    def _embed_post(self, block, depth: int) -> str:
        caption = self._caption(getattr(block, "caption", None), depth)
        author = getattr(block, "author", None) or ""
        detail = " ".join(
            part
            for part in (f"by {author}" if author else "", getattr(block, "url", ""))
            if part
        )
        head = _placeholder("embedded post", detail=detail, caption=caption)
        body = self.blocks(getattr(block, "blocks", None), depth=depth)
        return f"{head}\n\n{_quote(body)}" if body else head

    def _gallery(self, block, depth: int) -> str:
        kind = _GALLERY_KINDS[_type_name(block)]
        count = len(getattr(block, "items", None) or [])
        caption = self._caption(getattr(block, "caption", None), depth)
        return _placeholder(f"{kind} of {count} items", caption=caption)

    def _map(self, block, depth: int) -> str:
        geo = getattr(block, "geo", None)
        latitude = _format_coordinate(getattr(geo, "lat", None))
        longitude = _format_coordinate(getattr(geo, "long", None))
        detail = f"{latitude}, {longitude}" if latitude and longitude else ""
        caption = self._caption(getattr(block, "caption", None), depth)
        return _placeholder("map", detail=detail, caption=caption)

    def _channel(self, block, depth: int) -> str:
        channel = getattr(block, "channel", None)
        title = getattr(channel, "title", None) or ""
        username = getattr(channel, "username", None)
        detail = " ".join(
            part for part in (title, f"(@{username})" if username else "") if part
        )
        return _placeholder("channel", detail=detail)

    def _cover(self, block, depth: int) -> str:
        return self.block(getattr(block, "cover", None), depth=depth)

    def _related_articles(self, block, depth: int) -> str:
        title = _one_line(self.inline(getattr(block, "title", None), depth=depth))
        lines = []
        for article in getattr(block, "articles", None) or []:
            url = getattr(article, "url", None) or ""
            label = getattr(article, "title", None) or url
            lines.append(f"- [{_link_label(label)}]({_link_target(url)})")
        return "\n\n".join(part for part in (title, "\n".join(lines)) if part)

    def _button_row(self, block, depth: int) -> str:
        buttons = [
            self._button(
                self.inline(getattr(button, "text", None), depth=depth),
                getattr(button, "type", None),
            )
            for button in getattr(block, "buttons", None) or []
        ]
        return " ".join(buttons)

    def _button(self, label: str, button_type) -> str:
        url = getattr(button_type, "url", None)
        if isinstance(url, str) and url:
            return f"[{_link_label(label)}]({_link_target(url)})"
        return f"[button: {label}]"

    ## Rich text
    def inline(self, rich_text, *, depth: int, leading: bool = False) -> str:
        """Render RICH_TEXT as inline Markdown.

        LEADING is set for the first node of a paragraph or list item, the
        only place a reference anchor can become a footnote definition.
        """
        if rich_text is None:
            return ""
        if isinstance(rich_text, str):
            return rich_text
        if self.too_deep(depth):
            return self.unsupported_marker(NESTING_TOO_DEEP)
        name = _type_name(rich_text)
        depth += 1
        wrapper = _INLINE_WRAPPERS.get(name)
        if wrapper is not None:
            inner = self.inline(getattr(rich_text, "text", None), depth=depth)
            if name == "TextSuperscript" and _FOOTNOTE_REF_RE.fullmatch(inner):
                return inner
            left, right = wrapper
            return _wrap(inner, left=left, right=right)
        if name in _INLINE_PASSTHROUGH:
            return self.inline(getattr(rich_text, "text", None), depth=depth)
        handler = _INLINE_RENDERERS.get(name)
        if handler is None:
            return self.unsupported_marker(name)
        return handler(self, rich_text, depth=depth, leading=leading)

    def _text_plain(self, rich_text, *, depth: int, leading: bool) -> str:
        return getattr(rich_text, "text", None) or ""

    def _text_empty(self, rich_text, *, depth: int, leading: bool) -> str:
        return ""

    def _text_concat(self, rich_text, *, depth: int, leading: bool) -> str:
        texts = list(getattr(rich_text, "texts", None) or [])
        return "".join(
            self.inline(text, depth=depth, leading=leading and index == 0)
            for index, text in enumerate(texts)
        )

    def _text_fixed(self, rich_text, *, depth: int, leading: bool) -> str:
        code = self.plain(getattr(rich_text, "text", None), depth=depth)
        if not code:
            return ""
        fence = _code_fence(code, minimum=1)
        padding = " " if code.startswith("`") or code.endswith("`") else ""
        return f"{fence}{padding}{code}{padding}{fence}"

    def _text_url(self, rich_text, *, depth: int, leading: bool) -> str:
        label = self.inline(getattr(rich_text, "text", None), depth=depth)
        url = getattr(rich_text, "url", None) or ""
        if url.startswith("#"):
            self.linked_anchors.add(url[1:])
            if url[1:] in self.footnotes:
                return f"[^{url[1:]}]"
        return _link(label, url)

    def _text_email(self, rich_text, *, depth: int, leading: bool) -> str:
        label = self.inline(getattr(rich_text, "text", None), depth=depth)
        email = getattr(rich_text, "email", None) or ""
        return label if label == email else _link(label, f"mailto:{email}")

    def _text_phone(self, rich_text, *, depth: int, leading: bool) -> str:
        label = self.inline(getattr(rich_text, "text", None), depth=depth)
        phone = getattr(rich_text, "phone", None) or ""
        return label if label == phone else _link(label, f"tel:{phone}")

    def _text_mention_name(self, rich_text, *, depth: int, leading: bool) -> str:
        label = self.inline(getattr(rich_text, "text", None), depth=depth)
        user_id = getattr(rich_text, "user_id", None)
        return _link(label, f"tg://user?id={user_id}")

    def _text_anchor(self, rich_text, *, depth: int, leading: bool) -> str:
        """An empty anchor is a link target; one with text is a reference.

        A reference that opens a paragraph or list item is a footnote
        definition in Markdown terms. Anywhere else only its text is kept.
        """
        name = getattr(rich_text, "name", None) or ""
        text = self.inline(getattr(rich_text, "text", None), depth=depth)
        if not text:
            return f'<a name="{_html_attr(name)}"></a>'
        if leading and _FOOTNOTE_NAME_RE.fullmatch(name):
            if name not in self.leading_references:
                self.leading_references.append(name)
            if name in self.footnotes:
                return f"[^{name}]: {text}"
        return text

    def _text_math(self, rich_text, *, depth: int, leading: bool) -> str:
        source = (getattr(rich_text, "source", None) or "").strip()
        return f"${source}$" if source else ""

    def _text_custom_emoji(self, rich_text, *, depth: int, leading: bool) -> str:
        return getattr(rich_text, "alt", None) or ""

    def _text_image(self, rich_text, *, depth: int, leading: bool) -> str:
        return "[image]"

    def _text_date(self, rich_text, *, depth: int, leading: bool) -> str:
        text = self.inline(getattr(rich_text, "text", None), depth=depth)
        if text:
            return text
        date = getattr(rich_text, "date", None)
        return date.isoformat() if isinstance(date, datetime.datetime) else ""

    def _text_button(self, rich_text, *, depth: int, leading: bool) -> str:
        label = self.inline(getattr(rich_text, "text", None), depth=depth)
        return self._button(label, getattr(rich_text, "type", None))

    def plain(self, rich_text, *, depth: int) -> str:
        """Return the characters of RICH_TEXT without formatting, for code."""
        if rich_text is None:
            return ""
        if isinstance(rich_text, str):
            return rich_text
        if self.too_deep(depth):
            return self.unsupported_marker(NESTING_TOO_DEEP)
        name = _type_name(rich_text)
        depth += 1
        if name == "TextPlain":
            return getattr(rich_text, "text", None) or ""
        if name == "TextConcat":
            return "".join(
                self.plain(text, depth=depth)
                for text in getattr(rich_text, "texts", None) or []
            )
        if name in _PLAIN_WRAPPERS:
            return self.plain(getattr(rich_text, "text", None), depth=depth)
        if name == "TextMath":
            return getattr(rich_text, "source", None) or ""
        if name == "TextCustomEmoji":
            return getattr(rich_text, "alt", None) or ""
        if name in ("TextEmpty", "TextImage"):
            return ""
        return self.unsupported_marker(name)


@dataclass(frozen=True)
class _MediaSpec:
    kind: str
    #: The field naming the document whose attributes refine KIND (a video
    #: can be an animation, audio a voice note), or None.
    id_field: Optional[str]


@dataclass(frozen=True)
class _MediaLabel:
    kind: str
    detail: str


@dataclass(frozen=True)
class _GridCell:
    text: str
    header: bool
    align: str
    is_filler: bool = False

    @classmethod
    def filler(cls) -> "_GridCell":
        return cls(text="", header=True, align="", is_filler=True)


def _span(value) -> int:
    return value if isinstance(value, int) and value > 1 else 1


def _gfm_table(grid: list) -> str:
    """Render GRID as a GFM pipe table.

    GFM needs a header row. The first row serves when all its cells are
    header cells; otherwise an empty header row is added. Column alignment
    comes from the body cells, because Telegram centres header cells even
    when the source asked for no alignment.
    """
    width = max((len(row) for row in grid), default=0)
    if not width:
        return ""
    rows = [row + [_GridCell.filler()] * (width - len(row)) for row in grid]
    if all(cell.header for cell in rows[0]):
        header, body = rows[0], rows[1:]
    else:
        header, body = [_GridCell.filler()] * width, rows
    separators = []
    for column in range(width):
        align = next(
            (row[column].align for row in body if not row[column].is_filler), ""
        )
        separators.append(_ALIGN_SEPARATORS[align])
    lines = [_gfm_row(header), "|" + "|".join(separators) + "|"]
    lines.extend(_gfm_row(row) for row in body)
    return "\n".join(lines)


def _gfm_row(cells: list) -> str:
    return "| " + " | ".join(cell.text for cell in cells) + " |"


def _link(label: str, url: str) -> str:
    if not url or label == url:
        return label
    if not label:
        return f"<{_link_target(url)}>"
    return f"[{_link_label(label)}]({_link_target(url)})"


def _explicit_item_number(item, *, fallback: int) -> int:
    value = getattr(item, "value", None)
    if isinstance(value, int):
        return value
    digits = (getattr(item, "num", None) or "").strip().rstrip(".)")
    if digits.isdigit():
        return int(digits)
    return fallback


_ALIGN_SEPARATORS = {"": "---", "center": ":---:", "right": "---:"}

_HEADING_LEVELS = {
    "PageBlockHeading1": 1,
    "PageBlockHeading2": 2,
    "PageBlockHeading3": 3,
    "PageBlockHeading4": 4,
    "PageBlockHeading5": 5,
    "PageBlockHeading6": 6,
    "PageBlockTitle": 1,
    "PageBlockSubtitle": 2,
    "PageBlockHeader": 2,
    "PageBlockSubheader": 3,
}

_MEDIA_KINDS = {
    "PageBlockPhoto": _MediaSpec(kind="photo", id_field=None),
    "PageBlockVideo": _MediaSpec(kind="video", id_field="video_id"),
    "PageBlockAudio": _MediaSpec(kind="audio", id_field="audio_id"),
    "PageBlockDocument": _MediaSpec(kind="document", id_field="document_id"),
}

_GALLERY_KINDS = {"PageBlockCollage": "collage", "PageBlockSlideshow": "slideshow"}

_LIST_BLOCKS = frozenset({"PageBlockList", "PageBlockOrderedList"})
_TEXT_LIST_ITEMS = frozenset({"PageListItemText", "PageListOrderedItemText"})
_BLOCK_LIST_ITEMS = frozenset({"PageListItemBlocks", "PageListOrderedItemBlocks"})

_BLOCK_RENDERERS = {
    "PageBlockParagraph": _Renderer._paragraph,
    "PageBlockFooter": _Renderer._paragraph,
    "PageBlockKicker": _Renderer._paragraph,
    **{name: _Renderer._heading for name in _HEADING_LEVELS},
    "PageBlockAuthorDate": _Renderer._author_date,
    "PageBlockPreformatted": _Renderer._preformatted,
    "PageBlockMath": _Renderer._math_block,
    "PageBlockDivider": _Renderer._divider,
    "PageBlockAnchor": _Renderer._anchor_block,
    "PageBlockUnsupported": _Renderer._unsupported_block,
    "PageBlockBlockquote": _Renderer._blockquote,
    "PageBlockPullquote": _Renderer._blockquote,
    "PageBlockBlockquoteBlocks": _Renderer._blockquote_blocks,
    "PageBlockDetails": _Renderer._details,
    "PageBlockThinking": _Renderer._thinking,
    "PageBlockList": _Renderer._list,
    "PageBlockOrderedList": _Renderer._ordered_list,
    "PageBlockTable": _Renderer._table,
    **{name: _Renderer._media for name in _MEDIA_KINDS},
    "PageBlockEmbed": _Renderer._embed,
    "PageBlockEmbedPost": _Renderer._embed_post,
    **{name: _Renderer._gallery for name in _GALLERY_KINDS},
    "PageBlockMap": _Renderer._map,
    "InputPageBlockMap": _Renderer._map,
    "PageBlockChannel": _Renderer._channel,
    "PageBlockCover": _Renderer._cover,
    "PageBlockRelatedArticles": _Renderer._related_articles,
    "PageBlockButtonRow": _Renderer._button_row,
}

_INLINE_WRAPPERS = {
    "TextBold": ("**", "**"),
    "TextItalic": ("*", "*"),
    "TextStrike": ("~~", "~~"),
    "TextMarked": ("==", "=="),
    "TextSpoiler": ("||", "||"),
    "TextUnderline": ("<u>", "</u>"),
    "TextSubscript": ("<sub>", "</sub>"),
    "TextSuperscript": ("<sup>", "</sup>"),
}

#: Entities the server detects by itself (so the text is the whole payload),
#: and TextDiff, whose ``text`` is the current version.
_INLINE_PASSTHROUGH = frozenset(
    {
        "TextAutoUrl",
        "TextAutoEmail",
        "TextAutoPhone",
        "TextMention",
        "TextHashtag",
        "TextCashtag",
        "TextBotCommand",
        "TextBankCard",
        "TextDiff",
    }
)

_INLINE_RENDERERS = {
    "TextPlain": _Renderer._text_plain,
    "TextEmpty": _Renderer._text_empty,
    "TextConcat": _Renderer._text_concat,
    "TextFixed": _Renderer._text_fixed,
    "TextUrl": _Renderer._text_url,
    "TextEmail": _Renderer._text_email,
    "TextPhone": _Renderer._text_phone,
    "TextMentionName": _Renderer._text_mention_name,
    "TextAnchor": _Renderer._text_anchor,
    "TextMath": _Renderer._text_math,
    "TextCustomEmoji": _Renderer._text_custom_emoji,
    "TextImage": _Renderer._text_image,
    "TextDate": _Renderer._text_date,
    "TextButton": _Renderer._text_button,
}

_PLAIN_WRAPPERS = frozenset(
    set(_INLINE_WRAPPERS)
    | _INLINE_PASSTHROUGH
    | {
        "TextFixed",
        "TextUrl",
        "TextEmail",
        "TextPhone",
        "TextMentionName",
        "TextAnchor",
        "TextDate",
        "TextButton",
    }
)
