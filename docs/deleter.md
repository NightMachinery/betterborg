# Deleter Plugin

`stdplugins/deleter.py` provides the admin-only `.del` command for removing
recent chat messages.

## Commands

- `.del N` deletes the last `N` messages visible to the userbot in the current
  chat.
- `.del s N` scans the last `N` messages, deletes messages authored by the
  userbot/admin account, and removes that account's reactions from scanned
  messages.
- `.delallself` uses Telegram's `channels.deleteParticipantHistory` admin
  action to delete all messages authored by the userbot/admin account in the
  current supergroup or channel.

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

`.delallself` repeatedly calls `DeleteParticipantHistoryRequest` until Telegram
returns an `offset` of `0`. Telegram may reject the command in chats where this
admin action is unavailable or where the userbot account lacks sufficient
permissions; those errors are printed in the server log.
