# Twin Files

A long chat answer reaches Telegram twice: as text, and as a `.md` file with
the same content. This page says how the bot tells that file apart, and where
it leaves the file out of the model's context.

## Terms

- **Twin file** (or twin): the file copy of an answer whose text was also
  delivered. `util.edit_message` sends one in `SendFileMode.ALSO` and
  `ALSO_IF_LESS_THAN` (an answer of 4000 characters or more, below the
  file-only threshold of 60000, or 8000 in groups).
- **File-only answer**: an answer at or past the file-only threshold. It is
  sent only as a file, and its text message reads "[sent as file]". It is not
  a twin, since nothing else carries the answer.
- **Window mode**: a context mode that takes a stretch of the chat rather than
  following replies: Last N, Until Separator, a topic's thread, and the recent
  messages used with the `.s` override. Smart mode resolves to Reply Chain or
  to one of these.

## Marking

A twin's caption starts with `constants.TWIN_FILE_MARKER`, two invisible
characters (U+200B, U+2060) before the usual title and description. It is
added only when every chunk of the text was delivered. When the text failed,
or only part of it went out, the file may be the only full copy, so it stays
unmarked and is read as before. File-only answers are never marked.

The marker differs from `BOT_META_INFO_PREFIX` in both directions on purpose.
Meta-prefixed messages are dropped in every mode, Reply Chain included, while a
twin must still count when someone replies to it.

`edit_message(..., twin_file_marker="")` turns marking off for a call.
Answers sent before marking existed carry no marker and keep counting as
before.
