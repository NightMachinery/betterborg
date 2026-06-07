# LLM Chat Reaction Hydration

`llm_chat` includes Telegram reaction metadata in model history when Telegram exposes it. Userbot sessions can refresh reactions retrospectively with `GetMessagesReactionsRequest`; bot sessions cannot, because Telegram rejects that method for bots and ordinary bot `get_messages(..., ids=...)` does not include reaction metadata.

Reaction history is part of `uniborg.history_util`. The same history subsystem that records message IDs also records observed reactions as message metadata, using Redis with the existing very-long expiry and bounded per-chat history when Redis is available, plus the same in-memory fallback when Redis is not available. `llm_chat` asks `history_util` to hydrate fetched messages before model-history conversion.

For bot sessions, `history_util` listens for live raw reaction updates (`UpdateBotMessageReaction`, `UpdateBotMessageReactions`, and `UpdateMessageReactions`) and stores them on the matching history item. Observed reactions survive process restarts when Redis is available; with memory fallback they survive only for the current process. Reactions that were never delivered to the bot still cannot be recovered through bot auth.

Bot reaction updates require the bot to be an administrator in the chat. The official Bot API also requires `message_reaction` and `message_reaction_count` in `allowed_updates` for long-polling/webhook delivery; this Telethon/MTProto bot does not call Bot API `getUpdates`/`setWebhook`, but it receives the MTProto equivalent only after Telegram delivers raw reaction updates to the bot session. Anonymous/count reaction updates can be delayed by a few minutes.

There is no official Bot API method to retrieve reactions for an arbitrary old message ID. The Bot API exposes live update objects plus methods to set/delete reactions, but not a retrospective `getMessageReactions` call.

The formatter appends aggregate counts such as `[Reactions: ❤️×2 👍]`; when actor-specific updates were observed, sender-specific `Alice reacted: ❤️` lines may also be included. Reaction history diagnostics are silent by default; set `REACTION_HISTORY_CACHE_VERBOSITY_MODE=print_each_update` to print lines beginning `HistoryUtil reaction_history_cache`, or use `debug`/`all` too.
