# LLM user metadata

`uniborg.llm_db` stores API-key timestamps and Telegram identity metadata in the
same SQLite database as user API keys. These APIs never expose API-key values.

`get_api_key_metadata(user_id)` returns `ApiKeyMetadata` entries ordered by
service. Each entry contains `service` and `last_set_at`. Older key rows retain a
null timestamp after migration. Every successful `set_api_key` call writes a new
UTC timestamp, including when the key value is unchanged.

Migration takes a SQLite immediate write lock before inspecting the old schema,
so concurrent bot processes cannot both attempt the additive column change.

User identity is scoped by `(bot_id, user_id)`, so bots sharing the database do
not overwrite each other's view of a Telegram user. Use:

```python
record_user_profile(
    bot_id,
    user_id,
    first_name,
    last_name,
    username,
    private_contact_at=None,
    refresh_identity=True,
)
get_user_profile(bot_id, user_id)
```

A normal identity refresh stores all three supplied identity values. Passing
`username=None` therefore clears a username removed on Telegram. When an event
proves private contact but has no reliable identity fields, pass
`refresh_identity=False`; this records contact without erasing known names.

`private_contact_at` retains the earliest supplied time. The SQLite upsert makes
that comparison atomically, including when multiple bot processes share the
database. Naive input datetimes are interpreted as UTC; returned timestamps are
UTC-aware datetimes.

Incoming private messages remain the primary contact evidence. When an older
profile has no contact timestamp, the Codex user panel performs a bounded,
read-only scan of the bot's Telegram dialog list. An exact private-user dialog is
positive evidence because a bot cannot initiate it; the scan records the current
UTC observation time and refreshes that user's identity. Group/channel dialogs,
other users, bots, admins, missing dialogs, and API failures do not mark contact.
The scan does not use the shared message-history cache and never sends a message
or chat action.
