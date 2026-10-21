# LLM Chat Prompting

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
