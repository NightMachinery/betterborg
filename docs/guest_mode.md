# Guest Mode

How this repo's bots answer when someone mentions them in a chat they are not
in: the flow, the safety rules, and the shared code in `uniborg/tg_raw.py` and
`uniborg/guest_util.py`. The Telegram side (limits, open questions, evidence)
is in [telegram_ai_apis.md](telegram_ai_apis.md), section 2.3.

## Terms

- **Guest mode**: the Bot API 10.0 feature that lets a bot answer where it was
  mentioned, in a private chat or group it is not a member of.
- **Guest query**: the `UpdateBotGuestChatQuery(query_id, message,
  reference_messages, qts)` update the bot receives.
- **Trigger**: the message that summoned the bot (`message`).
- **Reference**: the message the trigger replies to (`reference_messages`, at
  most one). The bot sees nothing else of the chat.
- **Caller**: the trigger's sender (`from_id`).
- **Guest answer**: the one inline message the bot may post for a query, via
  `messages.setBotGuestChatResult`. The bot can then edit it, but never send
  anything else into that chat.
- **Echo**: in groups, the bot receives its own guest answer back as a new
  outgoing message with `guestchat_via_from` set to the caller.
- **Explicit call**: a trigger that mentions the bot. A reply to a guest answer
  also summons the bot, without a mention: an **implicit call**.

## The flow

1. Telethon delivers the update to a `events.Raw(UpdateBotGuestChatQuery)`
   handler that `guest_util.register_guest_handler` installed.
2. `guest_query_from_update` builds a `GuestQuery`: the trigger and references
   bound to a `GuestClient`, the caller, the chat kind and a thread key.
3. The wrapper drops the query if its trigger is forwarded, sent via a bot,
   or sent by a Business bot in its owner's name (such a message carries the
   owner as its sender, so it would pass for the owner's own command), older
   than the handler's `max_age_seconds`, or already claimed (a
   `QueryClaims` keyed by the bot's id and the query id, in Redis when
   available, so bots sharing a Redis never drop each other's queries).
4. The bot's handler decides whether to answer, answers once with
   `tg_raw.answer_guest` (a placeholder before any slow work), then edits the
   answer through `tg_raw.InlineEditor` until it is final.

## Safety rules

Each rule is enforced in code; this is why.

- **Never read or send by the guest chat id.** In a private chat the trigger's
  `peer_id` is the *other* participant as the caller sees it, and message ids
  are local to the caller's side. `get_messages(chat_id, …)` would read the
  bot's own DM with that person, and `send_message(chat_id, …)` would post
  there: a privacy leak. Guest messages are bound to `GuestClient`, an
  allow-list proxy (downloads, entity lookups and parse modes only) that
  raises `GuestContextError` for anything else. That includes Telethon's
  `_reload_message`, which it would otherwise use to find a "min" sender;
  guest messages get a no-op instead. Downloads are handed the message's
  media, not the message (`guest_util.download_target`), since Telethon
  refetches a message by (chat, id) when a file reference expires mid-download.
- **The caller is `from_id`, and only a user can be a caller.** `caller_id`
  is None when the trigger was posted as a channel.
- **One answer, sent once.** `setBotGuestChatResult` has been seen posting
  several copies when retried. `tg_raw.send_once` sends it straight to the
  sender (Telethon's `_call` would re-send it after server errors and flood
  waits) and marks it at-most-once, so the safety net in
  `telethon_safety.py` fails it with `DeliveryUnknownError` instead of
  re-sending it after a reconnect. A failed answer means giving up on the
  query, and doing none of its work.
- **Answer first, work after.** Telegram rejects answers after an
  undocumented deadline (about a minute has worked). Handlers answer with a
  placeholder, then do the slow part and edit.
- **Echoes are not commands and not admins.** `is_guest_answer` recognises an
  echo (`guestchat_via_from` set on an outgoing message).
- **Admin checks look at the caller only.** `util.isAdmin` treats an echo as
  never admin, and in a guest context counts only the caller's user id or
  username: a private trigger arrives with `out` set, and its "chat" is the
  other participant. Outside guest mode, chat-level trust (`adminChats`, a
  chat's username) now applies only to groups and channels, and to a private
  chat that is the sender's own.
- **Automation does not summon the shell.** Telegram finds the mention in
  plain text, so any message sent from the owner's account that starts with
  `@somebot .a …` would run as the owner, including a userbot's LLM answer
  steered by someone else, or command output echoing untrusted text.
  `guest_util.OutgoingTriggerGuardMixin` (first in `Uniborg`'s bases) replaces
  that "@" with "＠" (U+FF20) in outgoing messages, captions and edits of user
  accounts, and drops a mention entity there. It matches the mention followed
  by any of spaces, commas and colons (or none) and then `.a` or `.k`, since
  Telegram ends a mention at any of them; that is wider than what the shell
  acts on, so the two cannot drift apart. Text typed in a Telegram app never passes
  through it. `borg_guest_trigger_guard=0` turns it off; an
  unknown value stops startup. Other tools logged in as the owner are not
  covered.
- **Guest queries survive gaps.** Telethon drops qts updates that
  `getDifference` recovers; the qts re-dispatch net hands them on (see
  [telethon_upgrade.md](telethon_upgrade.md)). Recovered queries can be old,
  which is what the age gate and the claims are for.

## Shared code

### `uniborg/tg_raw.py`

- `send_once(client, request)`: one send, no retries, marked at-most-once.
- `answer_guest(client, *, query_id, title, text=None, entities=None,
  markdown=None, buttons=None, link_preview=False)` returns the guest
  answer's `InputBotInlineMessageID`. Exactly one of `text` and `markdown`
  (rich, parsed by Telegram's server). The title is mandatory
  (`ARTICLE_TITLE_EMPTY` otherwise) but never shown.
- `InlineEditor(client, inline_id)`: `async with` it, then
  `await editor.edit(text=…, entities=…, parse_mode=…, markdown=…, media=…,
  buttons=…)`. It borrows the sender for the inline id's data center once
  (edits from any other data center fail with `MESSAGE_ID_INVALID`) and
  returns False when Telegram says nothing changed. Inline edits cannot upload
  files, so `media` must be a file Telegram already has.
  `await editor.upload_photo(data, file_name=…)` makes one: it uploads through
  the bot's own data center with `messages.uploadMedia` on `InputPeerSelf`,
  which stores the photo and sends nothing (seen on the canary), and returns
  the `InputMediaPhoto` to pass as `media`.

### `uniborg/guest_util.py`

- `register_guest_handler(client, handler, *, claims, max_age_seconds=120,
  allow_forwarded=False, logger=None)`: call it from a plugin's module body.
  It registers nothing on a user account or on a Telethon without guest
  types, and the callback takes the handler's module name, so a plugin reload
  removes it.
- `GuestQuery`: `query_id`, `trigger`, `references`, `album_items` (the
  rest of the trigger's album, when `AlbumBatcher` folded its queries in),
  `triggers` (the trigger and `album_items`, in order), `messages`
  (references then triggers), `caller_id`, `chat_kind` (`ChatKind.PRIVATE` or `GROUP`),
  `thread_key` (`pair:<lo>:<hi>` of the two users in a private chat, which
  does not flip with who summons; `chat:<id>` in a group), `text`, `client`.
  Rich messages (the Premium editor, our own rich answers) read as the
  Markdown they render, through `tg_format.flatten_rich_message`.
- Mentions: `mentions(text, username=…)`, `text_after_leading_mention(text,
  username=…)`, and `strip_mention(message, username=…)`, which removes one
  leading or trailing mention in place, moving entities in UTF-16 units.
- `is_guest_message`, `is_guest_answer`, `is_guest_event`, `caller_id_of`.
- `QueryClaims(backend=redis_claim_backend(redis_util.get_redis))`: claim a
  key once per TTL, across restarts with Redis (keys under
  `borg:guest:claim:`), in memory without it.
- `GuestThreadStore(get_redis=redis_util.get_redis)`: recent answer records
  per thread (`add`, `records`, newest first), with `find_answer` (the record
  answered closest to a date, within a tolerance), `find_seen` (the newest
  record whose turns hold a message, by its `MessageFingerprint`, listed
  with `seen_entry`) and `answer_chain`
  (a record and the ones it continued, oldest first). Every bound guest
  message carries its fingerprint (`fingerprint_of`), taken before a handler
  strips the mention.
- Albums: `AlbumBatcher().collect(query, prefer=…)` folds the queries of one
  album (same thread, caller and `grouped_id`) into the first, which waits
  until a second passes with no new item (5 s at most), and returns None for
  the others. The lead is the first query `prefer` accepts, else the
  earliest item. `album_note(query)` is the line an answer ends with when an
  album item came without the rest of its album.
- `CallLimiter(backend=redis_counter_backend(redis_util.get_redis))`:
  `await limiter.allow(key, limit=n)` counts a call and says whether it is
  within `n` per hour. Windows follow the wall clock, so Redis (keys under
  `borg:guest:count:`) keeps the counts across restarts.
- `GuestEvent(query, text=…)`: a stand-in for a `NewMessage` event, for code
  written against events. `chat_id` is the synthetic string
  `guest:<thread_key>`, so per-chat settings and caches get their own
  namespace; `is_private` is False and `is_guest` is True; every method that
  would reply or read the chat raises `GuestContextError`.
- `GuestAnswerMessage(editor)`: a stand-in for the response `Message` that
  streaming code edits, including `util.edit_message`. Edits are at least 1.2 s
  apart; a flood wait that Telethon did not sleep through (over 60 s) blocks
  partial edits until it ends, and `finalize(...)` waits it out for the last
  edit. `reply`, `respond`, `get_chat` and `delete` raise, so text past the
  first 4096 UTF-16 units is dropped, never posted elsewhere.

## The guest shell (`stdplugins/advanced_get.py`)

`@<bot> .a CMD`, sent to the bot of the `stdplugins` instance (julia), runs
`CMD` the way `.a` does in a chat with the bot, with the same flags (`.aa`
without Brish, `.af` without forking, `.ad` without albums, `.an` with
`noglob`). The handler keeps queries for at most 60 seconds; a later one is
dropped, never run late.

- **Only a strict trigger runs.** The text must start with the bot's mention,
  then whitespace, then `.a` or `.k` (`guest_util.shell_command_after_mention`),
  and no code block may cover the mention. `@bot: .a` or `@bot.a` gets the
  usage line instead. The caller must be an admin by user id
  (`util.is_admin_by_id`).
- **`@<bot> .k` stops** the caller's own running guest commands of this guest
  chat (`shell_stream.visible` with the query's `thread_key`), with the forms
  of `.k` (alone, `N`, `all`, `ls`; docs/shell_streaming.md). Its answer is a
  note (`guest_util.answer_note`), so it costs one message in the chat. `.k`
  in the caller's private chat with the bot sees these commands too.
- **Every explicit call is answered.** A non-admin who mentions the bot gets
  "Not available here."; an admin whose text is not a strict trigger gets the
  usage line. A reply to the answer without a mention (an implicit call) gets
  nothing.
- **No answer, no execution.** The handler posts "⏳ Running…" first. If that
  fails, including `DeliveryUnknownError`, the command does not run: it must
  not run unseen, or twice.
- **Media**: the trigger's and the reference's files are downloaded into the
  command's working directory, as with `.a` on a reply. Of an album, only
  the item replied to arrives, and the answer's footer says so
  (`album_note`).
- **The answer** is the output as plain text, as `.a` sends it, cut to fit
  one message and followed by the exit code when it is not 0. A command
  still running after 2 s shows its output live in the answer, as `.a` does
  in a chat, with "@<bot> .k to stop" in its header (docs/shell_streaming.md,
  "Live guest answers"); one that ends sooner changes the answer once. A
  stopped command's answer adds "⏹ Stopped" under the exit code. Output is
  shown as a terminal would show it unless the caller turned the renderer
  off in `/settings`. Empty output reads "The process exited N.". An
  exception becomes the traceback.
- **Files go to the caller's DM**, after a header message naming the command:
  the files the command left in its working directory, plus `output.txt` with
  the whole output when it was cut (`output-<random>.txt` when the command
  made an `output.txt` of its own). The answer says how many were sent. They
  never go to the guest chat, whose id in a private chat is the other person.
  The caller must have started the bot for the DM to work; the answer says so
  when it does not.
- **A single file is also attached to the answer** when the output and the
  footer fit a caption (1024 UTF-16 units). An inline edit cannot upload, so
  the answer reuses the DM copy, with the output as its caption. Longer output
  keeps the text answer, which shows up to 4096 units, and the file is only
  in the DM; so does a refused attachment. Several files are only in the DM.

## The chat bot (`llm_chat_plugins/llm_chat.py`)

`@vlm_chat_bot hi` in a chat the bot is not in answers there, as the caller's
own chat with the bot would: with their default model, reasoning effort and
API key. Model and reasoning prefixes (`.f`, `.th`, …) work as in a private
chat. The handler keeps queries for at most 120 seconds.

- **Who may use it** is the guest policy below. Admins are exempt from the
  hourly limit.
- **Every explicit call is answered.** With the policy off, the caller gets
  "Guest answers are turned off"; with `admins`, a non-admin gets "Not
  available here."; over the hourly limit, the caller is told so. Implicit
  calls get none of these.
- **A caller without an API key is invited** to start the bot, with a button
  to `t.me/<bot>?start=guest`: on every explicit call, and on implicit calls
  at most once a day (a claim on `invite:<bot id>:<caller>`). With `invite:
  false`, an explicit call gets "Not available here." instead.
- **What it reads**: the trigger, with the mention removed, and the reference.
  Nothing else of the chat. A reply quote that would need a fetch is dropped,
  since `GuestClient` refuses the fetch.
- **An album is one call.** An album sent as a reply to a guest answer
  brings one query per item, in the same second. `AlbumBatcher` answers the
  first (the item that mentions the bot, if any, else the earliest) with
  every item, and leaves the others unanswered; they expire. A reference that
  is an album item comes alone: Telegram sends no other item of it, and
  nothing may fetch them, so the answer ends with a line saying so
  (`album_note`), as it does for an album item that arrived as the trigger
  with no other item. The line is not stored with the answer.
- **Replies continue the exchange.** Each final answer is stored as a
  *record*: the turns it answered, the record it continued (`parent`), the
  caller's id, and a fingerprint of each message its turns hold (our own
  answers aside). Records live in a per-thread list
  (`borg:guest:thread:<bot id>:<thread key>`, newest 500), kept until 7 days
  after the thread's latest answer. A reply finds the record it continues in
  one of three ways:
  - **A reply to one of our answers.** A reference is one of our answers when
    its sender is this bot (`guestchat_via_from` marks every guest bot's
    answer, so it counts only for a message with no sender). Its date picks
    the stored answer posted within 5 seconds of it. Without a match, the
    reference alone is read, as the assistant's turn.
  - **A reply to an earlier question**, from anyone: the question is on the
    reply path, so its exchange comes back.
  - **A reply to what a question replied to** (that record's reference, say a
    photo), from the same caller only. Another caller replying to the same
    photo asks a question beside that exchange, not after it, so they start
    fresh, as they would in a group's reply chain.

  Message ids cannot identify those messages, since each side of a private
  chat numbers messages on its own, so the record is found by the message's
  **fingerprint** (`guest_util.message_fingerprint`): its Unix date, a hash of
  its text and media (the photo or document id, which tells album items
  apart), and a hash of its sender. Date and content must match, and so must
  the sender when both copies name one (the other side's copy of a private
  message can lack it). An edited message has new content and is not
  recognised: the reply starts fresh, with the edited text. A continuation
  lists only its own question, since its reference is in the chain already,
  so each message belongs to one record, and a second reply to a question
  does not pull in the first reply's branch.

  The chain of records it continued then replaces the reference in the
  history, by its `parent` links, with no limit of its own: like a private
  reply chain, it stops at `HISTORY_MESSAGE_LIMIT` (1000) turns, keeping the
  newest. Other answers in the same chat are never added, so the history
  follows the replies, not a window of recent messages. Every chain of a
  thread shares its 500 records, so in a busy group other chains can push the
  oldest part of a long chain out. The stored turns can quote other people's
  messages, which is why they expire. The fingerprints are hashes, not the
  messages; the sender hash is a pseudonym, though, since a Telegram user id
  is short enough to recover from it.
- **Their media comes back too.** A guest message's file reference cannot be
  refreshed, so every file a guest answer downloads is also kept on disk, in
  the media store (`uniborg/media_store.py`), as is the image a guest answer
  showed. A stored turn keeps such a file as its key
  (`{"type": "media", "key": …}`), and a continuation rebuilds the part for
  the model that answers it, through `_process_media`, as if the message were
  new: capability checks and the Gemini Files API apply as usual. A file the
  store no longer has, or media that could not be kept, becomes "[media]".
  Codex reads no images in an assistant turn, so it does not see an answer's
  own image when continuing; Gemini does.
- **Generated images become the answer's photo.** An image model answers as
  itself, and `.i` is checked and resolved as in a private chat
  (`_image_generation_model`): without image generation access, an explicit
  call is told so and nothing runs. Each image, preview or final, is uploaded
  with `upload_photo` and edited into the answer (`GuestAnswerMessage.
  show_image`), so a later image replaces an earlier one and the last one
  stays. A preview is skipped during a flood wait; a final image waits it out.
  While the answer shows a photo, its text is the caption: streaming edits are
  cut to 1024 UTF-16 units, and the last edit is classic Markdown cut to that
  limit with the truncation note, empty when the model wrote nothing.
  Editing an answer into an uploaded photo has not yet been seen live.
- **The answer** starts as "💭 Thinking…", streams as classic Markdown edits
  (the first 4096 UTF-16 units), and ends as one rich Markdown edit, which the
  server renders: headings, tables, LaTeX. The prompt says so
  (`RICH_MARKDOWN_PROMPT`, with `GUEST_CHAT_PROMPT` in place of the group
  etiquette). Past 32000 UTF-8 bytes the answer is cut with a note. If
  Telegram refuses the rich edit, the answer is sent as classic Markdown.
- **Errors** never carry the "admin only" details
  (`llm_util.may_show_admin_details`): the answer is public. A Codex usage
  limit gets one line instead of the quota panel, which needs buttons and a
  message of its own. A model that returns nothing is retried twice, not
  thirty times.
- **Privacy**: the log records the caller, the model and the lengths, not the
  text; guest answers are not written to the conversation logs that `/log`
  sends. Media cache keys of guest messages include the caller, since their
  chat and message ids are the caller's view of the chat.

## The STT bot (`stt_plugins/stt.py`)

A mention of the bot in a reply to a voice note, audio, video or image, in a
chat the bot is not in, posts the transcript there, made with the caller's
own Gemini key. Media in the trigger itself counts too.

- **Only an explicit mention is a request.** A reply to a transcript without
  a mention gets nothing.
- **Every explicit call is answered**: "No voice note, audio, video or image
  in your message or the one you replied to." when there is nothing to
  transcribe; an invite with a start button when the caller has no Gemini
  key (checked before anything is downloaded); a note past 30 transcripts per
  caller per hour (admins are exempt).
- **The answer** starts as "🎙 Transcribing…", which also shows retry
  progress, and ends as classic Telegram Markdown when the transcript fits one
  message. The transcript prompt asks for Telegram Markdown (`__italic__`),
  which rich Markdown would read as bold. A longer transcript ends as rich
  Markdown, cut at 32000 UTF-8 bytes; if Telegram refuses that, as classic
  Markdown cut to one message.
- **Of an album, only the item replied to arrives**, and the transcript
  ends with a line saying so (`album_note`).
- **Guest transcripts are not logged**, unlike private ones
  (`~/.borg/stt/log/`): they are other people's media.
- The same checks as in a private chat (`prepare_stt_job`) come after the
  download; a failure is told in the answer.

## The chat bot's guest policy

The chat bot reads its guest policy from the optional `guest` section of
`~/.borg/llm_chat_config.json5` (or `LLM_CHAT_CONFIG_PATH`), the file that
holds Codex access ([codex_models.md](codex_models.md)). It is reloaded when
the file changes.

```json5
guest: {policy: "onboarded", max_calls_per_hour: 30, invite: true},
```

- `policy`: who may use the bot through guest mentions. `"onboarded"` (the
  default when the section is absent) means callers who have set an API key,
  as in a private chat with the bot; `"admins"` means bot admins only; `"off"`
  turns guest answers off.
- `max_calls_per_hour`: answers per caller per hour, from 1 to 10000; default
  30.
- `invite`: whether a caller the policy refuses is invited to start the bot;
  default true.

A malformed section (an unknown key, policy or type) turns guest mode off and
logs the error; the rest of the file still applies. A file that does not parse
at all also turns guest mode off, as it turns Codex off, so a typo elsewhere
cannot undo `policy: "off"`.

## What is kept in Redis

Every key is in the bot user's `borg:` namespace ([redis.md](redis.md)). When
Redis is unreachable, each falls back to the process's memory, which a restart
forgets.

- `borg:guest:claim:q:<bot id>:<query id>`, for a day: the query was handled,
  so a redelivered copy is dropped. All three bots.
- `borg:guest:claim:invite:<bot id>:<caller>`, for a day: the chat bot invited
  this caller after an implicit call.
- `borg:guest:count:chat:<bot id>:<caller>:<hour>` and
  `borg:guest:count:stt:<bot id>:<caller>:<hour>`, for an hour: the caller's
  calls in the current wall-clock hour (`<hour>` is Unix time divided by
  3600).
- `borg:guest:thread:<bot id>:<thread key>`, the newest 500 records, kept
  until 7 days after the latest answer: the chat bot's answers, for
  continuation.

## What is kept on disk

The media store, one SQLite file at `~/.borg/media_store.sqlite3` (or the
path in `borg_media_store_path`), holds the files of guest messages the chat
bot answered and the images its guest answers showed, keyed as the media cache
keys them (`guest_…`; an answer's image is `guest_answer_<record id>`). Files
are stored as bytes, not Base64.

- A file expires 7 days after it was stored or last read, so a continuation
  keeps the files of its chain alive.
- A file over 20 MiB is not kept. Past 1 GiB in total, the least recently
  used files go first.
- Expired files are deleted when a bot that answers guest queries starts and
  every hour after (`register_guest_handlers`; a plugin reload replaces the
  loop). SQLite reuses the space they free; the file does not shrink.
- Every call runs in a worker thread on a connection of its own, so the
  instances on one machine can share the file. A failing store is logged and
  treated as empty.
- The files are other people's media, which is why they expire. The test run
  points `borg_media_store_path` at a temporary directory (`tests/conftest.py`).

## Enabling guest mode for a bot

1. Run the instance on Telethon 1.45.0 (`.tgcaps`: `layer: 229`,
   `guest_types: True`), with the safety nets on.
2. Turn on Guest Mode in the BotFather Mini App; there is no command or API
   method for it. `.tgcaps` then shows `guest_enabled: True`.
3. If the bot also has inline mode, a mention at the very start of a message
   opens its inline popup. Turn inline mode off unless the bot needs it.
4. A bot that is a member of a group gets no guest queries there.
