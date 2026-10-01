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
3. The wrapper drops the query if its trigger is forwarded or sent via a bot,
   older than the handler's `max_age_seconds`, or already claimed (a
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
  guest messages get a no-op instead.
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
  accounts, and drops a mention entity there. Text typed in a Telegram app
  never passes through it. `borg_guest_trigger_guard=0` turns it off; an
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

### `uniborg/guest_util.py`

- `register_guest_handler(client, handler, *, claims, max_age_seconds=120,
  allow_forwarded=False, logger=None)`: call it from a plugin's module body.
  It registers nothing on a user account or on a Telethon without guest
  types, and the callback takes the handler's module name, so a plugin reload
  removes it.
- `GuestQuery`: `query_id`, `trigger`, `references`, `messages` (references
  then trigger), `caller_id`, `chat_kind` (`ChatKind.PRIVATE` or `GROUP`),
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
  answered closest to a date, within a tolerance) and `answer_chain` (a record
  and the ones it continued, oldest first).
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
  followed directly by `.a`, and no code block may cover the mention. The
  caller must be an admin by user id (`util.is_admin_by_id`).
- **Every explicit call is answered.** A non-admin who mentions the bot gets
  "Not available here."; an admin whose text is not a strict trigger gets the
  usage line. A reply to the answer without a mention (an implicit call) gets
  nothing.
- **No answer, no execution.** The handler posts "⏳ Running…" first. If that
  fails, including `DeliveryUnknownError`, the command does not run: it must
  not run unseen, or twice.
- **Media**: the trigger's and the reference's files are downloaded into the
  command's working directory, as with `.a` on a reply.
- **The answer** is the output as plain text, as `.a` sends it, cut to fit
  one message and followed by the exit code when it is not 0. Empty output
  reads "The process exited N.". An exception becomes the traceback.
- **Files go to the caller's DM**, after a header message naming the command:
  the files the command left in its working directory, plus `output.txt` with
  the whole output when it was cut. The answer says how many were sent. They
  never go to the guest chat, whose id in a private chat is the other person.
  The caller must have started the bot for the DM to work; the answer says so
  when it does not.
- **A single file is also attached to the answer.** An inline edit cannot
  upload, so the answer reuses the DM copy, with the output as its caption
  (1024 UTF-16 units, so less of it shows than in a text answer). If Telegram
  refuses, the answer stays text. Several files are only in the DM.

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
- **Replies continue the exchange.** Each final answer is stored with the
  turns it answered (text only, media as "[media]") and the answer it
  continued, for 7 days, in a per-thread list (`borg:guest:thread:<bot id>:
  <thread key>`, newest 50). When the reference is one of our answers, its
  date picks the stored answer posted within 5 seconds of it, and up to 10
  exchanges of that chain replace the reference in the history. Without a
  match, the reference alone is read, as the assistant's turn. The stored
  turns can quote other people's messages, which is why they expire.
- **No generated images**: an image model is replaced by the default model,
  and `.i` is ignored. A guest answer can only reuse files Telegram already
  has.
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
- `borg:guest:thread:<bot id>:<thread key>`, the newest 50 records, kept until
  7 days after the latest answer: the chat bot's answers, for continuation.

## Enabling guest mode for a bot

1. Run the instance on Telethon 1.45.0 (`.tgcaps`: `layer: 229`,
   `guest_types: True`), with the safety nets on.
2. Turn on Guest Mode in the BotFather Mini App; there is no command or API
   method for it. `.tgcaps` then shows `guest_enabled: True`.
3. If the bot also has inline mode, a mention at the very start of a message
   opens its inline popup. Turn inline mode off unless the bot needs it.
4. A bot that is a member of a group gets no guest queries there.
