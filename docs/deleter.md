# Deleter Plugin

`stdplugins/deleter.py` provides the admin-only `.del` command for removing
recent chat messages.

## Commands

- `.del N` deletes the last `N` messages visible to the userbot in the current
  chat.
- `.del s N` scans the last `N` messages, deletes messages authored by the
  userbot/admin account, and removes that account's reactions from scanned
  messages.
- `.delalltext N` scans up to the last `N` messages in the current chat and
  deletes text-only messages and conservative text-file attachments regardless
  of sender, except messages whose text is only whitespace-separated hashtags.
- `.delallself` uses Telegram admin actions to delete all reactions and
  messages authored by the userbot/admin account in the current supergroup or
  channel.
- `.delallselfreactions` uses Telegram's `messages.deleteParticipantReactions`
  admin action to delete all reactions added by the userbot/admin account in the
  current chat.

Reaction removal is selective. The plugin checks the reaction metadata already
present on each fetched message and only sends a Telegram `SendReactionRequest`
when the message is marked as having a reaction chosen by the current account.
If Telegram marks the reaction metadata as reduced with `min`, the plugin
batches those message IDs into chunked `GetMessagesReactionsRequest` calls
first, then only sends clear requests for messages confirmed to have a reaction
from the current account. It does not send a clear request for every scanned
message. If Telegram returns a flood wait while clearing reactions, the plugin
waits and retries instead of continuing to send more clear requests during the
wait window.

All message-deleting commands write an export session under
`~/tmp/tlg-deleter/DeleteSession_<command>_<chat_id>_<YYYY-MM-DD_HH-MM-SS>/`.
`result.json` uses the shared Telegram-style export format, and
`deleted_messages.wip.jsonl` is appended and flushed before each message is
deleted so partial exports remain useful if the process is killed. Text files
deleted by `.delalltext N` are downloaded into `files/` and referenced from the
exported JSON. Set `BORG_DELETER_EXPORT_DIR` to override the root directory.

`.delalltext N` scans up to the last `N` messages visible to the userbot using
explicit ID-window pagination. A message is considered text-only when it has
non-empty text and no Telethon media object. Attached text files are also deleted
when Telegram reports a `text/*` MIME type or the file suffix is one of `.txt`,
`.md`, `.markdown`, `.org`, `.rst`, or `.log`. Messages such as
`#xyzpic #lobby #sth` are kept because every token is a hashtag; mixed text like
`hello #lobby` is deleted.

`.delallself` first calls the same participant reaction deletion used by
`.delallselfreactions`, then repeatedly calls `DeleteParticipantHistoryRequest`
until Telegram returns an `offset` of `0`. Telegram may reject either admin
action in chats where it is unavailable or where the userbot account lacks
sufficient permissions; those errors are printed in the server log. Reaction
deletion failure does not stop message deletion. Because
`DeleteParticipantHistoryRequest` deletes server-side without returning message
contents, `.delallself` writes a metadata-only deletion export.

`.delallselfreactions` uses a raw MTProto request that is present in Telegram
Android but not yet exposed by Telethon 1.43.2. Telegram may reject the command
server-side if deleting the current account's own reactions through the admin
action is not allowed.

Uniborg registers a narrow Telethon compatibility parser for Telegram's newer
`TL_message` constructor `0x95ef6f2b`. This avoids noisy update parsing errors
after bulk reaction deletion while Telethon's generated schema is behind
Telegram Android. Remove the shim once Telethon exposes that constructor.
