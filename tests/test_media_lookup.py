import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
from telethon import errors

from test_llm_chat_guest import plugin
from uniborg import media_lookup, prompts


def test_prompt_file_changes_are_visible_to_the_next_read(tmp_path, monkeypatch):
    monkeypatch.setattr(prompts, "DIRECTORY", tmp_path)
    prompt = tmp_path / "example.md"
    prompt.write_text("First", encoding="utf-8")
    assert prompts.read("example") == "First"
    replacement = tmp_path / "replacement.md"
    replacement.write_text("Second", encoding="utf-8")
    replacement.replace(prompt)
    assert prompts.read("example") == "Second"


def test_both_shortcuts_compose_with_a_model_prefix():
    for text in (".media Example", ".f .imdb Example", ".s .media Example"):
        assert plugin._is_media_lookup(plugin._detect_and_process_message_prefix(text))
    assert not plugin._is_media_lookup(plugin._detect_and_process_message_prefix(".mediawiki Example"))


def test_history_export_shows_the_media_instructions_for_the_lookup(tmp_path):
    media_turn = SimpleNamespace(
        id=1, text=".f .imdb Wildwood", _role="user", media=None, forward=None,
        reply_to_msg_id=None, reactions=None,
    )
    export = SimpleNamespace(text="/asfile", sender_id=22, is_private=True)
    prefs = SimpleNamespace(metadata_mode="only_forwarded")
    with patch.object(plugin.user_manager, "get_prefs", return_value=prefs):
        model_history, history = [
            asyncio.run(plugin._process_turns_to_history(
                export, [media_turn], tmp_path, {}, "", "",
                is_private=True, export_mode=export_mode, include_system_prompt_p=False,
            ))[0]
            for export_mode in (False, True)
        ]
    assert model_history == history
    output = plugin._format_conversation_history(history, "markdown", "basic")
    assert "Media lookup: Wildwood" in output
    assert "## Media lookup instructions" in output
    assert prompts.read("magic/media") in output


def test_history_export_shows_media_instructions_when_command_is_only_quoted(tmp_path):
    media_turn = SimpleNamespace(
        id=1, text=".media Wildwood", _role="user", media=None, forward=None,
        reply_to_msg_id=None, reactions=None, date=None,
        get_sender=AsyncMock(return_value=SimpleNamespace(username="filmfan")),
    )
    answer = SimpleNamespace(
        id=2, text="Short profile.", _role="assistant", media=None, forward=None,
        reply_to_msg_id=1, reactions=None, chat_id=22,
        client=SimpleNamespace(get_messages=AsyncMock(return_value=media_turn)),
    )
    export = SimpleNamespace(text="/asfile", sender_id=22, is_private=True)
    prefs = SimpleNamespace(metadata_mode="only_forwarded")
    with patch.object(plugin.user_manager, "get_prefs", return_value=prefs):
        for turns in ([answer], [media_turn, answer]):
            model_history, history = [
                asyncio.run(plugin._process_turns_to_history(
                    export, turns, tmp_path, {}, "", "",
                    is_private=True, export_mode=export_mode, include_system_prompt_p=False,
                ))[0]
                for export_mode in (False, True)
            ]
            assert model_history == history
            output = plugin._format_conversation_history(history, "markdown", "basic")
            assert "Assistant Message" in output
            assert "> .media Wildwood" in output
            quote = "\n".join(
                line[1:].removeprefix(" ") for line in output.splitlines() if line.startswith(">")
            )
            assert prompts.read("magic/media") in quote


def test_rich_delivery_retries_rejected_poster_and_keeps_the_score_link():
    text = (
        "## Example\n![Poster](https://image.tmdb.org/t/p/w500/poster.jpg)\n"
        "<img src=https://example.org/photo.jpg>\n"
        "| Source | Score | Basis |\n| --- | --- | --- |\n"
        "| [IMDb](https://www.imdb.com/title/tt1234567/) | 8/10 | Audience |"
    )
    editor = AsyncMock(side_effect=[errors.BadRequestError(request=None, message="IMAGE_INVALID"), True])
    assert asyncio.run(media_lookup.deliver(editor, text))
    rich, retry = (call.args[0] for call in editor.await_args_list)
    assert "![Poster]" in rich and "![Poster]" not in retry
    assert "<img" not in rich
    assert "| [IMDb]" in retry
    classic = media_lookup.classic(text)
    assert "• [IMDb]" in classic and "| ---" not in classic


def test_profile_moves_prose_citations_to_footer_and_keeps_rating_links():
    fest = "[Festival](https://fest.afi.com/film/)"
    studio = "[Studio](https://www.laika.com/film/)"
    rating = "[IMDb](https://www.imdb.com/title/tt1234567/)"
    text = (
        f"## Example\n\nAnimated fantasy · 90 min ({fest})\n\n"
        f"| منبع | امتیاز |\n| :--- | ---: |\n| {rating} | 8/10 |\n\n"
        f"- **Studio** {studio}. ({fest})\n\n{rating} · {fest}"
    )
    rich = media_lookup.markdown(text)
    assert "Animated fantasy · 90 min\n" in rich
    assert "- **Studio** Studio." in rich
    assert f"| {rating} | 8/10 |" in rich
    assert rich.endswith(f"\n\n{fest} · {studio}")
    assert rich.count(fest) == rich.count(rating) == 1
    classic = media_lookup.classic(text)
    assert f"• {rating} · 8/10" in classic
    assert "منبع" not in classic
    assert classic.endswith(f"\n\n{fest} · {studio}")


def test_profile_embeds_a_single_approved_poster_without_leaking_rejected_urls():
    unapproved = "https://resizing.flixster.com.example.org/poster.jpg"
    approved = "https://resizing.flixster.com/image/test/v2/poster.jpg"
    second = "https://image.tmdb.org/t/p/w500/poster.jpg"
    text = f"## Example\n![Unapproved]({unapproved})\n![Poster]({approved})\n![Extra]({second})"
    assert media_lookup.markdown(text) == f"## Example\n\n![Poster]({approved})"
    assert media_lookup.classic(text) == "**Example**"


def test_profile_drops_plot_paragraph_and_places_imdb_first():
    rt = "[Rotten Tomatoes](https://www.rottentomatoes.com/m/example)"
    mc = "[Metacritic](https://www.metacritic.com/movie/example/)"
    imdb = "[IMDb](https://www.imdb.com/title/tt1234567/ratings/)"
    text = (
        "## Example (2026)\n\nStop-motion · Fantasy · 2h · Eerie woodland\n\n"
        "A hero goes looking for a missing sibling.\nAnother plot detail.\n\n"
        f"| Source | Score |\n| --- | --- |\n| {rt} | 97% |\n"
        f"| {mc} | 87/100 |\n| {imdb} | 8.6/10 |\n\n"
        "- **Director** An Example"
    )
    for profile in (media_lookup.markdown(text), media_lookup.classic(text)):
        assert "Eerie woodland" in profile
        assert "missing sibling" not in profile and "Another plot detail" not in profile
        assert profile.index(imdb) < profile.index(rt) < profile.index(mc)


def test_artwork_is_loaded_from_the_title_page_and_embedded():
    poster = "https://resizing.flixster.com/example.jpg"
    page = "https://www.rottentomatoes.com/m/example"
    text = (
        "## Example (2026)\n\nStop-motion · Fantasy\n\n"
        "| Source | Score |\n| --- | --- |\n"
        f"| [Rotten Tomatoes]({page}?tracking=1) | 97% |\n"
        "| [Unrelated](https://example.org/m/example) | 8/10 |"
    )
    requested = []

    def handle(request):
        requested.append(str(request.url))
        return httpx.Response(200, text=(
            '<meta property="og:image" content="https://example.org/wrong.jpg">'
            f'<meta content="{poster}" name="twitter:image">'
        ))

    client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
    send = AsyncMock()
    with patch.object(media_lookup.httpx, "AsyncClient", return_value=client):
        assert asyncio.run(media_lookup.deliver(send, text))
    send.assert_awaited_once()
    assert f"![Poster]({poster})" in send.await_args.args[0]
    assert requested == [page]


def test_chat_rich_edit_can_fall_back_to_one_new_reply():
    event = SimpleNamespace(id=9)
    response = SimpleNamespace(delete=AsyncMock())
    with (
        patch.object(plugin.tg_raw, "edit_rich_markdown", AsyncMock(
            side_effect=errors.BadRequestError(request=None, message="RICH_MESSAGE_INVALID"),
        )),
        patch.object(plugin.tg_raw, "send_rich_markdown", AsyncMock()) as send,
        patch.object(plugin, "_thread_topic_id", return_value=4),
    ):
        profile = "## Example\n\n![Poster](https://image.tmdb.org/t/p/w500/poster.jpg)"
        assert asyncio.run(plugin._deliver_media_profile(event, response, profile))
    send.assert_awaited_once_with(event, profile, topic_id=4, reply_to=9)
    response.delete.assert_awaited_once_with()
