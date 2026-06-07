# LLM Chat Reaction Hydration

`llm_chat` includes Telegram reaction metadata in model history when Telegram exposes it. Userbot sessions can refresh reactions retrospectively with `GetMessagesReactionsRequest`; bot sessions cannot, because Telegram rejects that method for bots and ordinary bot `get_messages(..., ids=...)` does not include reaction metadata.

For bot sessions, `llm_chat` listens for live raw reaction updates (`UpdateBotMessageReaction`, `UpdateBotMessageReactions`, and `UpdateMessageReactions`) and keeps an in-memory cache keyed by chat/message. When history is built, cached reactions are copied onto the selected messages before `_process_turns_to_history()` formats them. This means bot reaction visibility is forward-looking from process startup/restart; old reactions that were never delivered as updates are not recoverable through bot auth.

Bot reaction updates require the bot to be an administrator in the chat. The official Bot API also requires `message_reaction` and `message_reaction_count` in `allowed_updates` for long-polling/webhook delivery; this Telethon/MTProto bot does not call Bot API `getUpdates`/`setWebhook`, but it receives the MTProto equivalent only after Telegram delivers raw reaction updates to the bot session. Anonymous/count reaction updates can be delayed by a few minutes.

There is no official Bot API method to retrieve reactions for an arbitrary old message ID. The Bot API exposes live update objects plus methods to set/delete reactions, but not a retrospective `getMessageReactions` call.

The formatter appends aggregate counts such as `[Reactions: ❤️×2 👍]`; when actor-specific updates were observed, sender-specific `Alice reacted: ❤️` lines may also be included. Reaction cache diagnostics are silent by default; set `REACTION_HISTORY_CACHE_VERBOSITY_MODE=print_each_update` to print lines beginning `LLM_Chat reaction_history_cache`, or use `debug`/`all` to include the `ic()` debug output too.
