# All Mentions

`stdplugins/all.py` provides the admin-only `.all`, `@all`, `.allf`, `@allf`,
`.allIDs`, and `@allIDs` commands for mentioning or listing chat participants.

The participant list excludes Telegram bot accounts and the Borg/userbot account
itself before batching messages, so skipped accounts do not consume mention batch
slots.
