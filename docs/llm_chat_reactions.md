# LLM Chat Reaction Hydration

`llm_chat` explicitly refreshes Telegram reaction metadata before converting selected messages into model history. Telethon history calls such as `get_messages()` and `iter_messages()` may return `Message` objects with `message.reactions is None` even when the messages have reactions.

Before `_process_turns_to_history()` runs, `llm_chat` calls Telegram's `GetMessagesReactionsRequest` for the final expanded message set, then copies each returned `UpdateMessageReactions.reactions` value back onto the matching `Message`. If the refresh fails, history generation continues without reaction text.

The existing reaction formatting then appends aggregate counts such as `[Reactions: ❤️×2 👍]`; when Telegram exposes the reaction list, sender-specific `Alice reacted: ❤️` lines may also be included.
