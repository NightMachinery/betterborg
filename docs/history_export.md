# History Export Plugin

`stdplugins/history_export.py` adds a silent admin command for exporting the
current Telegram chat to a Telegram Desktop-style `result.json`.

## Usage

```text
.export
.export all
.export 100
```

`.export` and `.export all` stream all messages visible to the logged-in
Telegram account. `.export N` exports the latest `N` messages. Collection runs
newest-to-oldest so interrupted exports keep the most recent messages, but
`result.json` is still written oldest-to-newest.

The command is silent in Telegram. Completion and failure details are printed
only in the server terminal. Long exports also print periodic terminal progress.
If `SIGINT` is received while an export is active, the plugin stops collecting
new messages and writes a partial `result.json` with the messages already
gathered. If no export is active, `SIGINT` is left to the process's normal
handler.

## Output

By default exports are written under:

```text
~/tmp/.borg/chat_exports/<chat name>/ChatExport_<YYYY-MM-DD>-<UNIXTIME_NS>/result.json
```

Set `BORG_HISTORY_EXPORT_DIR` to override the root directory.

The JSON uses the same broad shape as Telegram Desktop exports:

```json
{
  "name": "Chat Name",
  "type": "personal_chat",
  "id": 123,
  "messages": []
}
```

Messages include text, text entities, sender ids, reply ids, forward/edit
metadata, reaction counts with available reactor identities, and media metadata
when available.

## Limitations

This is a text-only export. Media is not downloaded; media fields such as
`file`, `photo`, and `thumbnail` contain a placeholder while preserving metadata
such as file name, size, MIME type, dimensions, duration, performer, and title
when Telethon exposes them.

The plugin formats and validates the output with `jq`, so `jq` must be
available on the host.
