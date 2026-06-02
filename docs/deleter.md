# Deleter Plugin

`stdplugins/deleter.py` provides the admin-only `.del` command for removing
recent chat messages.

## Commands

- `.del N` deletes the last `N` messages visible to the userbot in the current
  chat.
- `.del s N` scans the last `N` messages, deletes messages authored by the
  userbot/admin account, and removes that account's reactions from scanned
  messages.

Reaction removal is selective. The plugin checks the reaction metadata already
present on each fetched message and only sends a Telegram `SendReactionRequest`
when the message is marked as having a reaction chosen by the current account.
It does not fetch full reaction lists or send a clear request for every scanned
message.
