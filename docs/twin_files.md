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

## Skipping

In every window mode, `build_conversation_history` drops the twins it finds,
through `history_util.is_twin_file`. A twin is skipped when all of these hold:

- its caption starts with the marker;
- it is a media message;
- this bot sent it (its sender id is the bot's, or it is an outgoing message
  with no sender id, as private chats deliver them);
- it is not a forward. A forwarded twin arrives without its text, so it is the
  only copy here and is kept.

The text head of the answer is a separate message and stays in the context,
so the model reads the answer once instead of twice. The skip applies even
when the window holds the twin but not its head (say, Last N starts right
after the head), which loses that one answer from the context.

Reply Chain never skips: a reply to a twin brings in only the question and the
twin, since the text head is a sibling, not a parent.

When "Include Reply Chain" merges the trigger's reply chain into a window,
the chain keeps its twins, as Reply Chain mode does. A twin the window dropped
comes back when the chain reaches it, which happens only when someone replies
to the twin (or to a reply to it): that reply asks about the file, so the file
should be there even if its text head is too.

`/asfile` exports go through the same builder, so they skip twins in window
modes and keep them in Reply Chain.

Last N counts a twin among its N messages before dropping it, so a window with
twins holds a few messages fewer than N.

`llm_chat._without_twin_files(messages, bot_id=...)` takes the bot's id as an
argument for tests; it defaults to the plugin's `BOT_ID`.
