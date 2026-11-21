# LLM Chat Prompting

## Media profiles

Use `.media Spirited Away` or `.imdb Disco Elysium` in Chat or as part of a
guest mention. For a shared title the bot picks the most likely work from
the conversation and search results. Add a medium, year, season, or platform
to select a specific release. The profile keeps the format, mood and setting
on one line and leaves all plot details out. A linked ratings table puts IMDb
first when verified. Short bullets cover cast and standout credits, and up to
three source links appear at the end.
Exact screen time appears only when there is a reliable public source.
The complete profile appears together when the layout is ready.
The model and history export use the same media expansion. A standalone
lookup carries the complete instructions. Reply quotes use the first 120
characters of the expanded text, plus an ellipsis when shortened. Exports
reconstruct the conversation using the current prompt files.

The shortcut requests web search on compatible Gemini, OpenAI, Codex, and
Antigravity routes. When the model supplies no approved artwork, Chat looks
for a poster on the linked Rotten Tomatoes, IMDb, MyAnimeList, or Steam title
pages. It sends one inline poster when available and can try a fresh rich
reply if editing the placeholder rejects the artwork. Classic Markdown
shows the scores as linked bullets.
See [Telegram rich messages](https://core.telegram.org/bots/api#rich-messages).

## Rich messages in history

Chat reconstructs Markdown from Telegram's rich blocks in model history,
reply quotes, and `/asfile` exports. This includes headings, lists, links,
and ratings tables. If Telegram sends a preview in a regular chat, Chat tries
to fetch the full message. When only the preview is available, the export marks
it as partial. Guest history uses the rich blocks included in the guest query.

## Prompt files and reloading

Repository prompts live in [`prompts/`](../prompts/), including the default
chat prompt, rendering and context instructions, magic shortcuts,
transcription, speech, title generation, and image instructions. Changes to
the Markdown take effect on the next request. Run-time user and chat prompts
continue to come from their settings.

Existing versioned shortcuts can read `prompts/legacy/<filename>.md` as a
repository override. They otherwise read `$NIGHTDIR/PE/<filename>.md`, so an
operator's external prompts and variants can stay private. Those files also
reload on their next use. The Python registry retains aliases and versions.

## Context metadata

`llm_chat` always appends a short context-metadata instruction to the effective system prompt. Injected sender metadata, reply quotes, media IDs, reaction summaries, and runtime context are model-visible grounding data, not wording or formatting to copy into final replies.

This instruction is appended for default, user-custom, and chat-custom system prompts so custom prompts do not accidentally make the model echo metadata labels such as `[Replying to ...]`.

## Bot usernames

The system prompt identifies the bot by its primary Telegram username and all
active additional usernames, deduplicated in profile order. It tells the model
that every listed name refers to itself. This context applies in private,
group and guest chats, including with custom system prompts; inactive aliases
are omitted. Username context can still be disabled with `include_username_p`.

Ordinary group activation also recognizes every active username, anywhere in
the message, case-insensitively and with complete mention boundaries. A leading
mention is stripped before parsing model shortcuts, including forms such as
`@alias: .f question`. Commands accept every active `/command@alias` suffix
and stay out of the conversational handler. Existing reply-activation modes,
forward filters and guest-answer echo filters still apply. Alias-only profiles
work without a primary username. Restart the bot after changing usernames.
