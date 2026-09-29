import ast
import importlib.util
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
import warnings

from telethon.tl import types

_ROOT = Path(__file__).resolve().parents[1]


def _load(name, relative_path):
    spec = importlib.util.spec_from_file_location(name, _ROOT / relative_path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


#: Loaded from their files so the `uniborg` package __init__ (which pulls in
#: uniborg.util, starts Brish servers and, on Telethon 1.45, fails) never runs.
tg_format = _load("tg_format_under_test", "uniborg/tg_format.py")
constants = _load("constants_under_test", "uniborg/constants.py")

flatten = tg_format.flatten_rich_message
FlattenedRichMessage = tg_format.FlattenedRichMessage
BOT_META_INFO_PREFIX = constants.BOT_META_INFO_PREFIX
BOT_META_INFO_LINE = constants.BOT_META_INFO_LINE

#: Rich messages arrived in layer 227 (Telethon 1.44); 1.43.2 has none of them.
HAS_RICH_TYPES = hasattr(types, "RichMessage")
NEEDS_RICH_TYPES = "needs the layer-227+ rich message types (Telethon >= 1.44)"


class Utf16HelperTests(unittest.TestCase):
    def test_utf16_len_counts_astral_characters_twice(self):
        self.assertEqual(tg_format.utf16_len(""), 0)
        self.assertEqual(tg_format.utf16_len("abc"), 3)
        self.assertEqual(tg_format.utf16_len("é"), 1)
        self.assertEqual(tg_format.utf16_len("😀"), 2)
        self.assertEqual(tg_format.utf16_len("a😀b"), 4)
        self.assertEqual(tg_format.utf16_len(BOT_META_INFO_PREFIX), 4)

    def test_text_that_fits_is_returned_unchanged(self):
        self.assertEqual(tg_format.truncate_utf16("hello", 5), "hello")
        self.assertEqual(tg_format.truncate_utf16("a😀", 3), "a😀")

    def test_the_suffix_counts_toward_the_limit(self):
        self.assertEqual(tg_format.truncate_utf16("hello world", 6), "hello…")
        self.assertEqual(
            tg_format.truncate_utf16("hello world", 8, suffix=" [+]"), "hell [+]"
        )
        self.assertEqual(tg_format.truncate_utf16("hello world", 4, suffix=""), "hell")

    def test_a_surrogate_pair_is_never_split(self):
        self.assertEqual(tg_format.truncate_utf16("ab😀cd", 4), "ab…")
        self.assertEqual(tg_format.truncate_utf16("ab😀cd", 5), "ab😀…")

    def test_a_suffix_longer_than_the_limit_is_returned_alone(self):
        self.assertEqual(tg_format.truncate_utf16("hello", 2, suffix="..."), "...")


def _llm_chat_utf16_helpers():
    """Compile llm_chat's own helpers without importing the plugin."""
    wanted = {"_truncate_utf16", "_utf16_units"}
    source = (_ROOT / "llm_chat_plugins" / "llm_chat.py").read_text()
    with warnings.catch_warnings():
        #: The plugin has string escapes the parser warns about.
        warnings.simplefilter("ignore", DeprecationWarning)
        tree = ast.parse(source)
    nodes = [
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name in wanted
    ]
    if {node.name for node in nodes} != wanted:
        return None
    namespace = {}
    module = ast.Module(body=nodes, type_ignores=[])
    exec(compile(module, "llm_chat.py", "exec"), namespace)
    return namespace


class Utf16ParityTests(unittest.TestCase):
    """The helpers must behave exactly like the llm_chat copies they replace."""

    def test_matches_llm_chat(self):
        helpers = _llm_chat_utf16_helpers()
        if helpers is None:
            self.skipTest("llm_chat no longer defines its own UTF-16 helpers")
        corpus = ["", "abc", "hello world", "a😀b😀c", "😀😀😀", "é" * 9, "x" * 40]
        for text in corpus:
            self.assertEqual(
                tg_format.utf16_len(text), helpers["_utf16_units"](text), text
            )
            for limit in range(0, 12):
                for suffix in ("…", "", "...", "😀"):
                    self.assertEqual(
                        tg_format.truncate_utf16(text, limit, suffix=suffix),
                        helpers["_truncate_utf16"](text, limit, suffix=suffix),
                        (text, limit, suffix),
                    )


class ImportLightTests(unittest.TestCase):
    def test_imports_only_the_standard_library(self):
        tree = ast.parse((_ROOT / "uniborg" / "tg_format.py").read_text())
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                self.assertEqual(node.level, 0, "relative imports load uniborg")
                imported.add(node.module.split(".")[0])
        self.assertLessEqual(imported, set(sys.stdlib_module_names))


##
_STAND_IN_CLASSES = {}


def _stand_in(type_name, **fields):
    """An object whose class is named TYPE_NAME, standing in for a TL type.

    The flattener dispatches on class names, so these run on Telethon 1.43.2,
    which lacks the rich types, and can impersonate constructors from layers
    that do not exist yet.
    """
    cls = _STAND_IN_CLASSES.setdefault(type_name, type(type_name, (), {}))
    obj = cls()
    obj.__dict__.update(fields)
    return obj


def _stand_in_paragraph(text):
    return _stand_in("PageBlockParagraph", text=_stand_in("TextPlain", text=text))


def _stand_in_rich(*blocks, part=False):
    return _stand_in("RichMessage", blocks=list(blocks), documents=[], part=part)


class DispatchByNameTests(unittest.TestCase):
    """Runs on every Telethon version."""

    def test_known_names_render_without_the_telethon_types(self):
        rich = _stand_in_rich(_stand_in_paragraph("one"), _stand_in_paragraph("two"))
        self.assertEqual(flatten(rich), "one\n\ntwo")

    def test_an_unknown_block_renders_a_marker_and_is_reported(self):
        seen = []
        rich = _stand_in_rich(
            _stand_in_paragraph("before"),
            _stand_in("PageBlockHologram", depth=3),
            _stand_in_paragraph("after"),
        )

        result = FlattenedRichMessage.from_rich(rich, on_unsupported=seen.append)

        self.assertEqual(
            result.text, "before\n\n[unsupported: PageBlockHologram]\n\nafter"
        )
        self.assertEqual(result.unsupported, ("PageBlockHologram",))
        self.assertEqual(seen, ["PageBlockHologram"])

    def test_an_unknown_rich_text_is_reported_once_per_name(self):
        sparkle = _stand_in("TextSparkle", text=_stand_in("TextPlain", text="x"))
        concat = _stand_in(
            "TextConcat", texts=[_stand_in("TextPlain", text="a "), sparkle, sparkle]
        )
        seen = []

        text = flatten(
            _stand_in_rich(_stand_in("PageBlockParagraph", text=concat)),
            on_unsupported=seen.append,
        )

        self.assertEqual(text, "a [unsupported: TextSparkle][unsupported: TextSparkle]")
        self.assertEqual(seen, ["TextSparkle"])

    def test_no_rich_message_flattens_to_nothing(self):
        result = FlattenedRichMessage.from_rich(None)
        self.assertEqual(
            (result.text, result.part, result.unsupported), ("", False, ())
        )

    def test_the_partial_flag_is_exposed(self):
        rich = _stand_in_rich(_stand_in_paragraph("start of a long answer"), part=True)
        self.assertTrue(FlattenedRichMessage.from_rich(rich).part)
        self.assertFalse(FlattenedRichMessage.from_rich(_stand_in_rich()).part)

    def test_hostile_nesting_is_cut_off_instead_of_recursing_forever(self):
        text = _stand_in("TextPlain", text="deep")
        for _ in range(5000):
            text = _stand_in("TextBold", text=text)

        result = FlattenedRichMessage.from_rich(
            _stand_in_rich(_stand_in("PageBlockParagraph", text=text))
        )

        self.assertIn("[unsupported: NestingTooDeep]", result.text)
        self.assertEqual(result.unsupported, (tg_format.NESTING_TOO_DEEP,))


##
def _plain(text):
    return types.TextPlain(text=text)


def _concat(*texts):
    return types.TextConcat(
        texts=[_plain(text) if isinstance(text, str) else text for text in texts]
    )


def _paragraph(*texts):
    return types.PageBlockParagraph(text=_concat(*texts))


def _rich(*blocks, documents=(), part=False):
    return types.RichMessage(
        blocks=list(blocks), photos=[], documents=list(documents), part=part
    )


def _cell(text, *, header=False, center=False, right=False, colspan=None, rowspan=None):
    return types.PageTableCell(
        header=header,
        align_center=center,
        align_right=right,
        valign_middle=True,
        text=_plain(text) if isinstance(text, str) else text,
        colspan=colspan,
        rowspan=rowspan,
    )


def _row(*cells):
    return types.PageTableRow(cells=list(cells))


def _caption(text, credit=None):
    return types.PageCaption(
        text=_plain(text) if text else types.TextEmpty(),
        credit=_plain(credit) if credit else types.TextEmpty(),
    )


def _document(document_id, *attributes):
    return types.Document(
        id=document_id,
        access_hash=0,
        file_reference=b"",
        date=None,
        mime_type="application/octet-stream",
        size=0,
        dc_id=0,
        attributes=list(attributes),
    )


#: The Markdown that the canary bot's guest-mode probe sent as a rich final
#: inline edit, with the values that run reported.
CANARY_SOURCE_MARKDOWN = "\n".join(
    [
        "## Guest probe ✅",
        "",
        "Answered in **0.11 s**, then streamed 11 inline edits (on the home DC).",
        "",
        "| Check | Result |",
        "|---|---|",
        "| Chat kind | supergroup or channel |",
        "| Replied-to messages received | 1 |",
        "| Reply header on trigger | MessageReplyHeader |",
        "| Edit flood waits | 0 |",
        "",
        "- [x] placeholder answer",
        "- [x] streaming edits",
        "- [x] rich final edit",
        "",
        "Math renders too: $a^2 + b^2 = c^2$",
        "",
        "```python",
        "print('code blocks keep their language')",
        "```",
    ]
)


def _canary_read_back():
    """The RichMessage Telegram returned for CANARY_SOURCE_MARKDOWN.

    Rebuilt field for field from the UpdateEditChannelMessage logged by the
    canary bot on 2026-09-29 (layer 229). Header cells come back centred even
    though the source asked for no alignment.
    """

    def cell(text, *, header):
        return types.PageTableCell(
            header=header,
            align_center=header,
            align_right=False,
            valign_middle=True,
            valign_bottom=False,
            text=_plain(text),
            colspan=None,
            rowspan=None,
        )

    rows = [
        ("Check", "Result"),
        ("Chat kind", "supergroup or channel"),
        ("Replied-to messages received", "1"),
        ("Reply header on trigger", "MessageReplyHeader"),
        ("Edit flood waits", "0"),
    ]
    return types.RichMessage(
        blocks=[
            types.PageBlockHeading2(text=_plain("Guest probe ✅")),
            types.PageBlockParagraph(
                text=types.TextConcat(
                    texts=[
                        _plain("Answered in "),
                        types.TextBold(text=_plain("0.11 s")),
                        _plain(", then streamed 11 inline edits (on the home DC)."),
                    ]
                )
            ),
            types.PageBlockTable(
                title=types.TextEmpty(),
                rows=[
                    _row(cell(a, header=index == 0), cell(b, header=index == 0))
                    for index, (a, b) in enumerate(rows)
                ],
                bordered=True,
                striped=True,
                compact=False,
            ),
            types.PageBlockList(
                items=[
                    types.PageListItemText(
                        text=_plain(text), checkbox=True, checked=True
                    )
                    for text in (
                        "placeholder answer",
                        "streaming edits",
                        "rich final edit",
                    )
                ]
            ),
            types.PageBlockParagraph(
                text=types.TextConcat(
                    texts=[
                        _plain("Math renders too: "),
                        types.TextMath(source="a^2 + b^2 = c^2"),
                    ]
                )
            ),
            types.PageBlockPreformatted(
                text=_plain("print('code blocks keep their language')"),
                language="python",
            ),
        ],
        photos=[],
        documents=[],
        rtl=False,
        part=False,
    )


@unittest.skipUnless(HAS_RICH_TYPES, NEEDS_RICH_TYPES)
class CanaryReadBackTests(unittest.TestCase):
    def test_the_read_back_flattens_to_the_markdown_that_was_sent(self):
        result = FlattenedRichMessage.from_rich(_canary_read_back())

        self.assertEqual(result.text, CANARY_SOURCE_MARKDOWN)
        self.assertFalse(result.part)
        self.assertEqual(result.unsupported, ())

    def test_each_feature_survives(self):
        text = flatten(_canary_read_back())

        self.assertTrue(text.startswith("## Guest probe ✅\n"))
        self.assertIn("**0.11 s**", text)
        self.assertIn("| Check | Result |\n|---|---|\n", text)
        self.assertIn("| Reply header on trigger | MessageReplyHeader |", text)
        self.assertIn("- [x] streaming edits", text)
        self.assertIn("$a^2 + b^2 = c^2$", text)
        self.assertIn("```python\nprint('code blocks keep their language')\n```", text)


@unittest.skipUnless(HAS_RICH_TYPES, NEEDS_RICH_TYPES)
class CoverageTests(unittest.TestCase):
    """Every constructor of the installed layer has a renderer.

    Unknown constructors only degrade to a marker at runtime, so this is what
    tells a Telethon upgrade that the flattener needs new cases.
    """

    def _constructors_like(self, example):
        return {
            name
            for name, value in vars(types).items()
            if getattr(value, "SUBCLASS_OF_ID", None) == example.SUBCLASS_OF_ID
            and hasattr(value, "CONSTRUCTOR_ID")
        }

    def test_every_page_block_has_a_renderer(self):
        self.assertLessEqual(
            self._constructors_like(types.PageBlockParagraph),
            set(tg_format._BLOCK_RENDERERS),
        )

    def test_every_rich_text_has_a_renderer(self):
        known = (
            set(tg_format._INLINE_WRAPPERS)
            | tg_format._INLINE_PASSTHROUGH
            | set(tg_format._INLINE_RENDERERS)
        )
        self.assertLessEqual(self._constructors_like(types.TextPlain), known)

    def test_every_list_item_has_a_renderer(self):
        known = tg_format._TEXT_LIST_ITEMS | tg_format._BLOCK_LIST_ITEMS
        self.assertLessEqual(
            self._constructors_like(types.PageListItemText)
            | self._constructors_like(types.PageListOrderedItemText),
            known,
        )


@unittest.skipUnless(HAS_RICH_TYPES, NEEDS_RICH_TYPES)
class BlockTests(unittest.TestCase):
    def test_headings_keep_their_level(self):
        blocks = [
            getattr(types, f"PageBlockHeading{level}")(text=_plain(f"H{level}"))
            for level in range(1, 7)
        ]
        self.assertEqual(
            flatten(_rich(*blocks)),
            "# H1\n\n## H2\n\n### H3\n\n#### H4\n\n##### H5\n\n###### H6",
        )

    def test_nested_lists_keep_checkbox_state(self):
        nested = types.PageBlockList(
            items=[
                types.PageListItemText(
                    text=_plain("done"), checkbox=True, checked=True
                ),
                types.PageListItemText(
                    text=_plain("todo"), checkbox=True, checked=False
                ),
            ]
        )
        outer = types.PageBlockList(
            items=[
                types.PageListItemBlocks(blocks=[_paragraph("parent"), nested]),
                types.PageListItemText(text=_plain("sibling")),
            ]
        )
        self.assertEqual(
            flatten(_rich(outer)),
            "- parent\n  - [x] done\n  - [ ] todo\n- sibling",
        )

    def test_ordered_lists_honour_start_reversal_and_explicit_values(self):
        def items(*texts):
            return [types.PageListOrderedItemText(text=_plain(t)) for t in texts]

        self.assertEqual(
            flatten(_rich(types.PageBlockOrderedList(items=items("a", "b"), start=3))),
            "3. a\n4. b",
        )
        self.assertEqual(
            flatten(
                _rich(
                    types.PageBlockOrderedList(
                        items=items("a", "b", "c"), reversed=True
                    )
                )
            ),
            "3. a\n2. b\n1. c",
        )
        explicit = [
            types.PageListOrderedItemText(text=_plain("a"), value=7),
            types.PageListOrderedItemText(text=_plain("b"), checkbox=True),
        ]
        self.assertEqual(
            flatten(_rich(types.PageBlockOrderedList(items=explicit))),
            "7. a\n8. [ ] b",
        )

    def test_ordered_item_blocks_indent_under_the_marker(self):
        item = types.PageListOrderedItemBlocks(
            blocks=[
                _paragraph("step"),
                types.PageBlockPreformatted(text=_plain("ls"), language="sh"),
            ]
        )
        self.assertEqual(
            flatten(_rich(types.PageBlockOrderedList(items=[item]))),
            "1. step\n\n   ```sh\n   ls\n   ```",
        )

    def test_quotes_keep_their_body_and_credit(self):
        quote = types.PageBlockBlockquote(
            text=_concat("line one\nline two"), caption=_plain("Someone")
        )
        pull = types.PageBlockPullquote(
            text=_plain("pulled"), caption=types.TextEmpty()
        )
        blocks = types.PageBlockBlockquoteBlocks(
            blocks=[_paragraph("first"), _paragraph("second")],
            caption=types.TextEmpty(),
        )
        self.assertEqual(
            flatten(_rich(quote, pull, blocks)),
            "> line one\n> line two\n>\n> <cite>Someone</cite>\n\n"
            "> pulled\n\n"
            "> first\n>\n> second",
        )

    def test_code_keeps_its_language_and_outfences_inner_backticks(self):
        code = "print('```')"
        block = types.PageBlockPreformatted(text=_plain(code), language="python")
        self.assertEqual(flatten(_rich(block)), f"````python\n{code}\n````")

    def test_math_blocks_keep_their_latex_source(self):
        block = types.PageBlockMath(source="E = mc^2")
        self.assertEqual(flatten(_rich(block)), "$$\nE = mc^2\n$$")

    def test_details_and_thinking_become_collapsible_html(self):
        details = types.PageBlockDetails(
            blocks=[_paragraph("hidden")],
            title=types.TextBold(text=_plain("More")),
            open=True,
        )
        thinking = types.PageBlockThinking(text=_plain("hmm"))
        self.assertEqual(
            flatten(_rich(details, thinking)),
            "<details open><summary>**More**</summary>\n\nhidden\n\n</details>\n\n"
            "<details><summary>Thinking</summary>\n\nhmm\n\n</details>",
        )

    def test_dividers_and_anchors(self):
        self.assertEqual(
            flatten(
                _rich(
                    _paragraph("a"),
                    types.PageBlockDivider(),
                    types.PageBlockAnchor(name="x"),
                )
            ),
            'a\n\n---\n\n<a name="x"></a>',
        )

    def test_media_render_as_placeholders_with_captions(self):
        documents = [
            _document(1, types.DocumentAttributeAnimated()),
            _document(2, types.DocumentAttributeAudio(duration=3, voice=True)),
            _document(3, types.DocumentAttributeFilename(file_name="report.pdf")),
        ]
        blocks = [
            types.PageBlockPhoto(photo_id=9, caption=_caption("A cat", "me")),
            types.PageBlockVideo(video_id=1, caption=_caption(None)),
            types.PageBlockAudio(audio_id=2, caption=_caption(None)),
            types.PageBlockDocument(document_id=3, caption=_caption("Q3")),
            types.PageBlockMap(
                geo=types.GeoPoint(long=13.405, lat=52.52, access_hash=0),
                zoom=12,
                w=100,
                h=100,
                caption=_caption("Berlin"),
            ),
            types.PageBlockEmbed(caption=_caption(None), url="https://example.com/v"),
            types.PageBlockCollage(
                items=[types.PageBlockPhoto(photo_id=9, caption=_caption(None))] * 2,
                caption=_caption(None),
            ),
            types.PageBlockChannel(
                channel=SimpleNamespace(title="News", username="news")
            ),
        ]
        self.assertEqual(
            flatten(_rich(*blocks, documents=documents)).split("\n\n"),
            [
                "[photo: A cat (me)]",
                "[animation]",
                "[voice note]",
                "[document report.pdf: Q3]",
                "[map 52.52, 13.405: Berlin]",
                "[embed https://example.com/v]",
                "[collage of 2 items]",
                "[channel News (@news)]",
            ],
        )

    def test_buttons_keep_urls(self):
        row = types.PageBlockButtonRow(
            buttons=[
                types.PageButton(
                    text=_plain("Open"),
                    type=types.InlineButtonTypeUrl(url="https://t.me/"),
                ),
                types.PageButton(
                    text=_plain("Press"), type=types.InlineButtonTypeCallback(data=b"x")
                ),
            ]
        )
        self.assertEqual(flatten(_rich(row)), "[Open](https://t.me/) [button: Press]")

    def test_a_block_the_server_marks_unsupported_is_reported(self):
        result = FlattenedRichMessage.from_rich(_rich(types.PageBlockUnsupported()))
        self.assertEqual(result.text, "[unsupported: PageBlockUnsupported]")
        self.assertEqual(result.unsupported, ("PageBlockUnsupported",))


@unittest.skipUnless(HAS_RICH_TYPES, NEEDS_RICH_TYPES)
class TableTests(unittest.TestCase):
    def test_alignment_comes_from_body_cells_and_pipes_are_escaped(self):
        table = types.PageBlockTable(
            title=_plain("Scores"),
            rows=[
                _row(
                    _cell("Name", header=True, center=True),
                    _cell("Score", header=True, center=True),
                ),
                _row(_cell("a|b"), _cell("1", right=True)),
                _row(
                    _cell("two\nlines"),
                    _cell(types.TextFixed(text=_plain("x|y")), right=True),
                ),
                _row(_cell("back\\|slash"), _cell("", right=True)),
            ],
        )
        self.assertEqual(
            flatten(_rich(table)),
            "Scores\n\n"
            "| Name | Score |\n"
            "|---|---:|\n"
            "| a\\|b | 1 |\n"
            "| two<br>lines | `x\\|y` |\n"
            "| back\\\\\\|slash |  |",
        )

    def test_a_table_without_header_cells_gets_an_empty_header(self):
        table = types.PageBlockTable(
            title=types.TextEmpty(),
            rows=[_row(_cell("a", center=True), _cell("b"))],
        )
        self.assertEqual(flatten(_rich(table)), "|  |  |\n|:---:|---|\n| a | b |")

    def test_spanning_cells_leave_the_covered_slots_empty(self):
        table = types.PageBlockTable(
            title=types.TextEmpty(),
            rows=[
                _row(_cell("wide", header=True, colspan=2), _cell("c", header=True)),
                _row(_cell("tall", rowspan=2), _cell("b1"), _cell("c1")),
                _row(_cell("b2"), _cell("c2")),
            ],
        )
        self.assertEqual(
            flatten(_rich(table)),
            "| wide |  | c |\n|---|---|---|\n| tall | b1 | c1 |\n|  | b2 | c2 |",
        )

    def test_a_hostile_colspan_is_clamped(self):
        table = types.PageBlockTable(
            title=types.TextEmpty(),
            rows=[_row(_cell("x", header=True, colspan=10**9))],
        )
        header = flatten(_rich(table)).split("\n")[0]
        self.assertEqual(header.count("|"), tg_format._MAX_TABLE_COLUMNS + 1)


@unittest.skipUnless(HAS_RICH_TYPES, NEEDS_RICH_TYPES)
class RichTextTests(unittest.TestCase):
    def assertInline(self, rich_text, expected):
        self.assertEqual(
            flatten(_rich(types.PageBlockParagraph(text=rich_text))), expected
        )

    def test_styles(self):
        cases = [
            (types.TextBold, "**x**"),
            (types.TextItalic, "*x*"),
            (types.TextUnderline, "<u>x</u>"),
            (types.TextStrike, "~~x~~"),
            (types.TextMarked, "==x=="),
            (types.TextSpoiler, "||x||"),
            (types.TextSubscript, "<sub>x</sub>"),
            (types.TextSuperscript, "<sup>x</sup>"),
        ]
        for cls, expected in cases:
            with self.subTest(cls.__name__):
                self.assertInline(cls(text=_plain("x")), expected)

    def test_edge_whitespace_stays_outside_delimiters(self):
        self.assertInline(
            _concat("a", types.TextBold(text=_plain(" b ")), "c"), "a **b** c"
        )

    def test_nested_styles(self):
        self.assertInline(
            types.TextBold(
                text=_concat("bold ", types.TextItalic(text=_plain("both")))
            ),
            "**bold *both***",
        )

    def test_inline_code_and_math(self):
        self.assertInline(types.TextFixed(text=_plain("a`b")), "``a`b``")
        self.assertInline(types.TextFixed(text=_plain("`")), "`` ` ``")
        self.assertInline(types.TextMath(source="x^2"), "$x^2$")

    def test_links_and_entities(self):
        def url(label, target):
            return types.TextUrl(text=_plain(label), url=target, webpage_id=0)

        self.assertInline(url("docs", "https://t.me/x y"), "[docs](https://t.me/x%20y)")
        self.assertInline(url("https://t.me/", "https://t.me/"), "https://t.me/")
        self.assertInline(url("[1]", "https://t.me/"), "[\\[1\\]](https://t.me/)")
        self.assertInline(
            types.TextEmail(text=_plain("write me"), email="a@t.me"),
            "[write me](mailto:a@t.me)",
        )
        self.assertInline(
            types.TextEmail(text=_plain("a@t.me"), email="a@t.me"), "a@t.me"
        )
        self.assertInline(
            types.TextPhone(text=_plain("call"), phone="+123"), "[call](tel:+123)"
        )
        self.assertInline(
            types.TextMentionName(text=_plain("Ann"), user_id=42),
            "[Ann](tg://user?id=42)",
        )
        self.assertInline(
            _concat(
                types.TextMention(text=_plain("@bot")),
                " ",
                types.TextBotCommand(text=_plain("/start")),
                " ",
                types.TextHashtag(text=_plain("#tag")),
            ),
            "@bot /start #tag",
        )

    def test_custom_emoji_keep_their_alternative_text(self):
        self.assertInline(
            _concat("ok ", types.TextCustomEmoji(document_id=5, alt="👍")), "ok 👍"
        )

    def test_footnotes_become_markdown_footnotes(self):
        reference = types.TextSuperscript(
            text=types.TextUrl(text=_plain("1"), url="#id1", webpage_id=0)
        )
        definition = types.PageBlockParagraph(
            text=types.TextAnchor(text=_plain("Definition."), name="id1")
        )
        self.assertEqual(
            flatten(_rich(_paragraph("Text with a reference", reference), definition)),
            "Text with a reference[^id1]\n\n[^id1]: Definition.",
        )

    def test_unlinked_references_and_plain_anchors(self):
        unlinked = types.PageBlockParagraph(
            text=types.TextAnchor(text=_plain("Referenced text"), name="note-1")
        )
        link = _paragraph(
            types.TextUrl(text=_plain("chapter"), url="#chapter-1", webpage_id=0)
        )
        anchor = types.PageBlockParagraph(
            text=types.TextAnchor(text=types.TextEmpty(), name="chapter-1")
        )
        self.assertEqual(
            flatten(_rich(unlinked, link, anchor)),
            'Referenced text\n\n[chapter](#chapter-1)\n\n<a name="chapter-1"></a>',
        )


@unittest.skipUnless(HAS_RICH_TYPES, NEEDS_RICH_TYPES)
class ZeroWidthTests(unittest.TestCase):
    def test_only_a_lone_zero_width_space_paragraph_is_dropped(self):
        rich = _rich(
            _paragraph("first"),
            _paragraph("\u200b"),
            _paragraph(BOT_META_INFO_PREFIX + "meta"),
            _paragraph(BOT_META_INFO_PREFIX),
            _paragraph("\u200b\u200b"),
            _paragraph(BOT_META_INFO_LINE),
        )
        self.assertEqual(
            flatten(rich).split("\n\n"),
            [
                "first",
                BOT_META_INFO_PREFIX + "meta",
                BOT_META_INFO_PREFIX,
                "\u200b\u200b",
                BOT_META_INFO_LINE,
            ],
        )


if __name__ == "__main__":
    unittest.main()
