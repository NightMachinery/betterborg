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
   `QueryClaims` keyed by query id, in Redis when available).
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
  allow_forwarded=False)`: call it from a plugin's module body. It registers
  nothing on a user account or on a Telethon without guest types, and the
  callback takes the handler's module name, so a plugin reload removes it.
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

## Enabling guest mode for a bot

1. Run the instance on Telethon 1.45.0 (`.tgcaps`: `layer: 229`,
   `guest_types: True`), with the safety nets on.
2. Turn on Guest Mode in the BotFather Mini App; there is no command or API
   method for it. `.tgcaps` then shows `guest_enabled: True`.
3. If the bot also has inline mode, a mention at the very start of a message
   opens its inline popup. Turn inline mode off unless the bot needs it.
4. A bot that is a member of a group gets no guest queries there.
