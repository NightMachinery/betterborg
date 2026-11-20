import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

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
        assert asyncio.run(plugin._deliver_media_profile(event, response, "## Example"))
    send.assert_awaited_once_with(event, "## Example", topic_id=4, reply_to=9)
    response.delete.assert_awaited_once_with()
