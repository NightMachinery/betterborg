# Draft Streaming

How the chat bot (`llm_chat_plugins/llm_chat.py`) streams an answer as a live
Telegram draft instead of by editing a message, and the `/stream` setting that
picks between the two. The Telegram side (the API, its limits, the client
quirks this works around) is in
[telegram_ai_apis.md](telegram_ai_apis.md), section 2.1. The code is
`uniborg/draft_stream.py`, and `uniborg/stream_driver.py` holds what the
streaming loops share.

## Terms

- **Edit streaming**: the old way. The bot sends a placeholder message ("...")
  and edits it as the answer grows. Each edit counts against Telegram's edit
  limits, so long answers slow to one edit per 15 s after 30 s, and one per
  minute after two.
- **Draft**: a temporary preview a bot shows in a private chat with
  `messages.setTyping` and `SendMessageTextDraftAction`. It causes no
  notification, vanishes 30 s after its last update, and the user's client
  replaces it with the real message when that arrives (**adoption**).
- **Draft streaming**: showing the growing answer as a draft, then sending the
  finished answer as one real message.
- **Stand-in**: `draft_stream.DraftAnswerMessage`, the object the streaming
  code edits in place of the placeholder message. It looks enough like a
  Telethon `Message` (`edit`, `reply`, `delete`, `id`, `text`) that the
  streaming loops of litellm, Codex and Pioneer, and `util.edit_message`, work
  on it unchanged.
- **Part**: one message of the finished answer. An answer longer than 4096
  characters is several messages, which `util.edit_message` builds by editing
  the first and replying to it with the rest; on the stand-in each of those
  becomes a part, sent for real only at the end.
- **Sync draft**: one last draft, equal to the first part, sent just before
  that part. Telegram Desktop adopts a draft only when the real message starts
  like it, so without this the draft can linger next to the answer.
- **Heartbeat**: a draft resent with the elapsed time ("⏳ 42s") when the
  answer has not changed for 20 s, so the draft does not expire while the model
  thinks.

## What the user sees

In a private chat, the bot first shows Telegram's own "Thinking…" (an empty
draft). The answer then grows in place about once a second, with the usual "▌"
cursor, and never slows down the way edit streaming does. When the model
finishes, the draft turns into the answer, which arrives as a normal message
replying to the question. A long answer shows only its last 4096 characters
while it streams, then arrives as all its messages.

On Telegram 10.3 clients with Telethon 1.45, the draft has a Stop button.
Pressing it cancels the generation, as `/stop` does, and whatever the draft
showed is sent as the answer.

## Settings: `/stream`

`/stream` (private chats only) shows the current settings and buttons to
change them. There are two scopes, each set to **Drafts** or **Edits**:

- **Private chats**: Drafts by default. This includes private-chat topics, where
  the draft shows only in its topic.
- **Groups**: Edits by default.

`/stream private edits` or `/stream groups drafts` sets a scope directly. The
settings are per user, kept with the other preferences (`stream_private`,
`stream_groups` in `UserPrefs`), and `/status` lists them. The modes
(`StreamMode`), the scope names and the choice of scope for a chat live in
`uniborg/stream_driver.py` (`stream_scope`, `stream_mode`, `set_stream_mode`),
so another plugin can offer the same setting with its own preferences.

The groups setting exists for when Telegram allows drafts there. Today it
refuses them (`TEXTDRAFT_PEER_INVALID`), so a group set to Drafts costs one
refused call, after which that chat streams by edits until a restart (see
Fallbacks).

Draft streaming needs a bot account. On the userbot, `/stream` says so and
every answer streams by edits; the reasons are in telegram_ai_apis.md (users'
drafts are never adopted on Android, yet still block the recipient's send
button). On a Telethon without the draft action, the menu says every answer
streams by edits.

## How it works

1. `chat_handler` asks `_response_placeholder` for the message to stream into.
   It decides whether to use drafts (the user's setting for this kind of chat
   is Drafts and the account is a bot), and `stream_driver.open_stream_target`
   does the rest: with drafts it builds a stand-in and calls `start`, which
   sends the first draft. Without drafts, or if `start` fails, it sends the
   usual placeholder message instead, so the answer streams by edits.
2. `_generate_streamed` runs the generation through
   `stream_driver.run_stoppable`, as its own task, and `stream_driver.stop_wired`
   points the stand-in's `on_stop` at that task's `cancel`, so the Stop button
   can cancel it. When the generation returns or fails, `stop_wired` calls
   `end_stream`. `stop_wired` takes any callable, so other work, such as a
   shell command, can be stopped the same way.
3. While streaming, an edit only records the new text and wakes the **draft
   worker**, a background task that sends the last live part as a draft, at
   most once per second (`DRAFT_MIN_INTERVAL`), and sends a heartbeat after
   20 s without changes (`DRAFT_HEARTBEAT_SECONDS`). Every streaming loop
   (litellm, Pioneer, Codex and native Gemini images) shows the answer
   through a `stream_driver.PacedEditor`, which edits only when an edit is
   due. The pace comes from `draft_stream.streaming_pace`: for a stand-in it
   is at most once a second with a plain cursor, and for a real message it is
   the slowing edit pace. Native Gemini images keep one pace at any age
   (`stream_driver.fixed_pace`): the model's streaming delay and a plain
   cursor.
4. `end_stream` cancels the worker and waits for it, so no draft can arrive
   after the answer and show as a ghost draft.
5. After `end_stream`, the final delivery edits the stand-in as it would a
   real message, and the first edit of each part sends it: a sync draft, then
   the real message (a reply to the question, or to the previous part).
6. `chat_handler`'s `finally` calls `stream_driver.flush_draft`, whose
   `flush` sends every part that was never edited after the stream ended. This is how an error message, a
   cancelled partial answer or the text left by Stop still reaches the chat.

An edit that carries buttons ends the stream at once and sends the part for
real, since a draft cannot carry buttons. An answer that turns out to be only
images deletes the stand-in, which sends no text at all.

## The Stop button

Where Telethon can build it (`draft_stream.STOP_SUPPORTED`, true on 1.45), each
draft is sent with `can_stop=True`. The press arrives as `UpdateUserTyping`
with `SendMessageStopDraftAction(random_id)`. The plugin registers a handler
for it with `stream_driver.register_draft_stop(borg, module=__name__)`, which
passes it to `draft_stream.on_typing_update`. That finds the stream by its
`random_id` (`_ACTIVE`) and acts only when the update comes from the user the
stream is answering. The handler is registered under the plugin's module name,
so a plugin reload removes it (`Uniborg.remove_events_of_mod`). Two plugins may
each register one: a stream stops only once.

After Stop, the stand-in sends no more drafts (clients would ignore them
anyway) and no sync draft. The press is not guaranteed to arrive, since that
update carries no pts; `/stop` still works.

On Telethon 1.43.2 the old draft action has no Stop button, so drafts are sent
without one.

## Flood waits and the call timeout

Drafts share Telegram's typing budget for the chat: 20 calls in 5 s and 40 in
30 s. One draft a second plus a heartbeat stays under it, but a flood wait can
still come back.

Telethon's own handling of a flood wait is wrong for drafts: it sleeps through
any wait of up to 60 s (it ignores a per-call `flood_sleep_threshold`) and then
sends the stale draft, which could land after the answer as a ghost. So every
draft call is wrapped in `asyncio.wait_for` with a 5 s timeout
(`DRAFT_CALL_TIMEOUT`). A call that takes longer is cancelled, which cancels
Telethon's sleep, and is treated as a flood wait of 5 s. On a flood wait the
worker holds the drafts until the wait is over, and the sync draft is skipped
while it lasts. The answer itself never waits for a draft.

## Fallbacks

- **A refused chat** (`TEXTDRAFT_PEER_INVALID`): `start` returns False and the
  chat id is remembered in `_REFUSED_CHATS`, so later answers there go
  straight to edit streaming, until the bot restarts.
- **Any other failure of the first draft**, a flood wait or the call timeout
  included: `start` returns False and only this answer streams by edits.
- **A failure later in the stream** is logged, and the next change tries
  again. The answer is sent either way.

## Known limits

- **An image-only answer can leave its draft up for 30 s.** No text message
  arrives to adopt the draft, so Telegram Desktop keeps it until it expires,
  and Android disables the send button while it lives.
- **Two answers streaming in one chat share one draft.** Clients keep one
  draft per sender and thread, so they overwrite each other until both finish.
  The finished answers are unaffected.
- **Images sent during a draft stream** use an upload action ("sending a
  photo…"), which also goes through `messages.setTyping`. Whether a client
  then drops the draft is untested.
- **Guest answers do not use drafts.** A guest answer is an inline message,
  and drafts exist only in private chats with the bot.

## Tests

- `tests/test_draft_stream.py`: the stand-in with a fake client. Drafts while
  streaming and one real message at the end, long answers, errors left in the
  draft, buttons, refused chats, flood waits, a hanging call, heartbeats, Stop,
  and `streaming_pace`.
- `tests/test_stream_driver.py`: `PacedEditor` on a scripted clock (the
  strict interval check, failed and unchanged edits, the pace tiers and the
  fixed pace), and on a real loop `stream_driver.follow`, the trailing-edge
  pump for producers that go quiet, such as a shell command (the chat bot's
  loops do not use it). Also the stream settings, the stream target's
  opening, Stop wiring and flush on their own, and the Stop handler's
  registration and its removal with the plugin.
- `tests/test_gemini_image_stream.py`: native Gemini images' partial edits
  keep one pace and cursor past 30 s.
- `tests/test_llm_chat_stream.py`: the plugin's choice of drafts or edits per
  scope, Stop cancelling the generation, and the `/stream` command, menu and
  buttons.

Not yet tested live: the sync draft's effect on adoption, heartbeats keeping
a draft alive, whether the server still accepts the 1.43.2 draft action, and
the Stop press reaching the bot.
