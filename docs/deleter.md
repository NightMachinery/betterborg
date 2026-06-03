# Deleter Plugin

`stdplugins/deleter.py` provides the admin-only `.del` command for removing
recent chat messages.

## Commands

- `.del N` deletes the last `N` messages visible to the userbot in the current
  chat.
- `.del s N` scans the last `N` messages, deletes messages authored by the
  userbot/admin account, and removes that account's reactions from scanned
  messages.
- `.delalltext` deletes all text-only messages in the current chat, regardless
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

`.delalltext` scans the full current chat history visible to the userbot. A
message is considered text-only when it has non-empty text and no Telethon media
object. Messages such as `#xyzpic #lobby #sth` are kept because every token is a
hashtag; mixed text like `hello #lobby` is deleted.

`.delallself` first calls the same participant reaction deletion used by
`.delallselfreactions`, then repeatedly calls `DeleteParticipantHistoryRequest`
until Telegram returns an `offset` of `0`. Telegram may reject either admin
action in chats where it is unavailable or where the userbot account lacks
sufficient permissions; those errors are printed in the server log. Reaction
deletion failure does not stop message deletion.

`.delallselfreactions` uses a raw MTProto request that is present in Telegram
Android but not yet exposed by Telethon 1.43.2. Telegram may reject the command
server-side if deleting the current account's own reactions through the admin
action is not allowed.

Uniborg registers a narrow Telethon compatibility parser for Telegram's newer
`TL_message` constructor `0x95ef6f2b`. This avoids noisy update parsing errors
after bulk reaction deletion while Telethon's generated schema is behind
Telegram Android. Remove the shim once Telethon exposes that constructor.
