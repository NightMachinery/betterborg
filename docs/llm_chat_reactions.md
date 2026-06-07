# LLM Chat Reaction Hydration

`llm_chat` includes Telegram reaction metadata in model history when Telegram exposes it. Userbot sessions can refresh reactions retrospectively with `GetMessagesReactionsRequest`; bot sessions cannot, because Telegram rejects that method for bots.

For bot sessions, `llm_chat` listens for live raw reaction updates (`UpdateBotMessageReaction`, `UpdateBotMessageReactions`, and `UpdateMessageReactions`) and keeps an in-memory cache keyed by chat/message. When history is built, cached reactions are copied onto the selected messages before `_process_turns_to_history()` formats them. This means bot reaction visibility is forward-looking from process startup/restart; old reactions that were never delivered as updates are not recoverable through bot auth.

The formatter appends aggregate counts such as `[Reactions: ❤️×2 👍]`; when actor-specific updates were observed, sender-specific `Alice reacted: ❤️` lines may also be included. Diagnostic `ic()` lines currently log cached reaction updates, cache application, and bot refresh skips so the tmux session can confirm whether Telegram is delivering reaction updates.
