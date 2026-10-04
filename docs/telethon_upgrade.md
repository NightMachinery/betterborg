# Telethon Upgrades and Telegram Layers

How this repo copes with Telegram's schema versions, what protects a running
bot from objects Telethon cannot parse, how to move from Telethon 1.43.2 to
1.45.0 without taking every instance down, how to build buttons that work on
both, and how to see what an instance can use (`.tgcaps`).

## Terms

- **Layer**: the version number of Telegram's TL schema. Each Telethon release
  is generated from one layer, exposed as `telethon.tl.alltlobjects.LAYER`.
  Telethon 1.43.2 is layer 224, 1.44.0 is layer 227, and 1.45.0 is layer 229.
- **Constructor**: the 32-bit id that starts every serialized TL object. It
  names both the type and its exact field layout, so a new field means a new
  constructor id.
- **Layer leak**: the server sends a constructor from a newer layer than the
  one the session declared.
- **Safety net**: one of the runtime guards in `uniborg/telethon_safety.py`
  that limit the damage of an unparseable object.
- **Canary**: a throwaway BotFather bot with its own token, its own `.session`
  file and its own virtualenv, used to try an upgrade before any production
  instance sees it.
- **Shared env**: the single Python environment that every production instance
  runs from. Installing a package there reaches every instance at its next
  restart.

## How the layer is chosen

The client declares its layer, not the server. On every connect Telethon wraps
`initConnection` in `invokeWithLayer(LAYER, ...)`. The MTProto docs say the
layer "will be saved with all other parameters of the client and any future
requests will be using this saved value." The server then answers in that
layer.

Newer content is usually downgraded to fit. A rich message sent by a newer
client can arrive on an older layer as empty text plus
`MessageMediaUnsupported` (Telethon Codeberg issue #48). On 1.43.2 some
messages from users and other bots may therefore look empty, and no error is
logged.

Layer leaks happen anyway, and this repo has had one. After the userbot called
`messages.deleteParticipantReactions`, a layer-225 method, on a layer-224
session, the server answered with the layer-225 `message#95ef6f2b`.
`uniborg/telethon_compat.py` registers a hand-written parser for that
constructor. It reads the layer-225 field `guestchat_via_from` (the peer that
invoked a guest bot) and throws it away. Outside reports show layer-218/220
`message` and layer-228 `user` objects reaching older Telethon clients during
ordinary reads on user accounts.

Constructor ids can also change within one layer number. tdesktop revised
layer 229 several times: `sendMessageTextDraftAction` went from `376d975c` to
`3630b85a`, and `ephemeralMessage` changed twice. "Layer 229" alone does not
pin the wire format. The 1.45.0 wheel carries the latest revision.

## Why one unknown object costs so much

TL objects carry no length prefix, so Telethon cannot skip an object whose
constructor it does not know. It raises `TypeNotFoundError` and loses the whole
enclosing object. In 1.43.2 and 1.45.0 (the relevant files are byte-identical):

- **A pushed message** is dropped and never acknowledged.
- **An entry of a `msg_container`** takes the whole container with it,
  including sibling updates, pongs and RPC results. A request whose result sat
  in that container can hang until a reconnect.
- **A `gzip_packed` container entry** stops the loop over the container, so
  the entries after it are never processed.
- **An RPC result** fails only that call. Telegram may already have executed
  it, so never retry such a call blindly.
- **A `getDifference` or `getChannelDifference` result** makes the update loop
  disconnect the client. `run_until_disconnected` then raises. In foreground
  mode the process exits; under the FastAPI server the HTTP side keeps running
  while the bot is offline.

The last case chains with the others: a dropped update that carried a `pts` or
`qts` leaves a gap, Telethon fetches the difference to fill it, and the
difference contains the same unknown object.

## The safety nets

`Uniborg.create` calls `telethon_safety.install_safety_nets` just before the
first connect. The nets, and the event kind each one records:

- **Container skip** (`container_entry`): replaces
  `MessageContainer.from_reader`. An entry that raises `TypeNotFoundError` is
  skipped using the entry's own length prefix and replaced by a
  `SkippedObject` placeholder. The placeholder's msg_id is still acknowledged,
  and Telethon's update handler logs and drops it. Siblings survive.
- **Message guard** (`processing`): wraps `MTProtoSender._process_message`, so
  a failing container entry (in practice a `gzip_packed` one) no longer stops
  the rest of the container. It wraps `_process_message` rather than
  `_handle_container` because the sender binds `_handle_container` into its
  handler table at construction, so a class patch would miss the main sender.
- **Log counter** (`dropped_message`): a handler on the
  `telethon.network.mtprotosender` logger that recognises Telethon's own
  `Type %08x not found` record, which is what a dropped non-container message
  produces. Without the message guard it also counts `processing` events from
  logged exceptions. It only sees INFO records while that logger's level is
  INFO or lower, which `stdborg.py` sets up.
- **Difference fallback** (`difference`, `channel_difference`,
  `rpc_result`): `Uniborg` mixes in `DifferenceFallbackMixin`, which overrides
  `__call__`. Telethon's update loop fetches differences through `self(...)`,
  so the override sees them.
  - For `getDifference`, it returns an empty `updates.Difference` carrying a
    fresh `getState`. Telethon applies it, jumps past the bad window and keeps
    running.
  - For `getChannelDifference`, it raises `ChannelPrivateError`. Telethon
    treats that as a ban: it forgets the channel's state, and the next update
    from the channel starts it afresh. Returning an empty channel difference
    instead would keep the gap and fail again on every later update.
  - Any other call is counted as `rpc_result` and re-raised unchanged.
  - Repeated fallbacks back off exponentially. The first runs at once, the
    repeats wait 1 s, 2 s, 4 s and so on up to 60 s, and the streak resets
    after 10 quiet minutes. Hammering `getDifference` would otherwise invite
    FLOOD_WAIT. The wait pauses all update processing, so each channel keeps
    its own streak: when many channels fail in one sweep, none of them waits
    behind the others.

Two more nets fix Telethon behaviour that loses or repeats things without any
parsing error. Guest mode depends on both (see [guest_mode.md](guest_mode.md)):

- **qts re-dispatch** (counted in `recovered`, not as an event): wraps
  `MessageBox.apply_difference_type`. Telethon applies a difference's final
  state before it runs `other_updates` through `process_updates`, so every
  qts update that `getDifference` recovered after a gap looks already handled
  and is dropped. TDLib applies them. The net hands on the qts updates that
  are newer than the qts held before the difference and whose type is in
  `REDISPATCH_UPDATE_NAMES` (`UpdateBotGuestChatQuery`, `UpdateBotStopped`),
  in qts order, after the rest of the difference.
  - The list is an allow-list because redelivery can repeat updates: session
    state is saved about once a minute, so after a hard stop the next
    difference may contain updates that were already dispatched. Only
    consumers that deduplicate belong on it. Reaction updates stay off it,
    since `history_util` merges their counts.
  - If the split itself fails, the difference is applied unchanged. An
    exception there would make `_update_loop` disconnect the client.
  - Re-dispatched updates are logged at INFO and counted in
    `SafetyStats.recovered`, which `summary()` prints. They send no alert.
- **At-most-once guard** (logged, not counted): wraps `MTProtoSender._reconnect`,
  which re-queues every sent but unanswered request under a fresh session.
  Telegram treats the re-sent copy as a new call, so a guest answer caught by
  a dropped connection would post twice. A request whose future was passed to
  `mark_at_most_once` (as `tg_raw.send_once` does) is removed first, and its
  future fails with `DeliveryUnknownError`: it may or may not have run, and it
  was not re-sent. Requests still in the send queue were never sent and are
  left alone. `at_most_once_installed()` says whether the guard is active.

Every event is logged as a warning starting with `Telegram safety net`, counted
per kind and per constructor in `borg.safety_stats` (a `SafetyStats`, whose
`summary()` gives one line), and alerted to the log chat. Alerts go out at
most once per kind every 10 minutes, and the next one says how many events
were held back. With no log chat the event is only logged, and a failed alert
send is logged too.

### Switching them off

`borg_tg_safety_nets` controls all of them. It is on when unset or empty. `0`,
`false`, `no` or `off` turns every net off, and `1`, `true`, `yes` or `on`
keeps them on. Any other value stops startup with a `ValueError`, so a typo
cannot silently disable them.

### Version guard

The container skip, the message guard, the qts re-dispatch and the
at-most-once guard replace private Telethon code, so they install only on the
versions they were checked against: 1.43.2 and 1.45.0. On any other version
they are skipped with a warning. The log counter
and the difference fallback install everywhere. The fallback only substitutes
answers Telegram itself could give (an empty difference, a private channel),
so it is safe on any version. If a future Telethon rewords its log line, the
counter simply stops matching. Before adding a version to
`SUPPORTED_TELETHON_VERSIONS`, diff these against 1.43.2:

- `tl/core/messagecontainer.py`;
- the `_recv_loop`, `_process_message`, `_handle_container`,
  `_handle_gzip_packed` and `_handle_update` methods of
  `network/mtprotosender.py`. The placeholder relies on `_handle_update`
  dropping anything that is not an `Updates`;
- `_update_loop` in `client/updates.py`, whose handling of
  `ChannelPrivateError` the channel fallback relies on, and which must still
  dispatch everything `apply_difference` returns;
- `_updates/messagebox.py`: `apply_difference_type`, `process_updates`,
  `apply_pts_info`, `set_state` and `PtsInfo.from_update`, which the qts
  re-dispatch mirrors;
- `_reconnect` in `network/mtprotosender.py` (it must still re-queue
  `_pending_state`), `MTProtoSender.send` (it must still return the request's
  future) and `network/requeststate.py`, for the at-most-once guard.

`uniborg/tg_compat.py` has no version guard, but it also leans on Telethon
internals. Check them on any new version: the private `Button._is_inline` in
`tl/custom/button.py`, which decides whether a row is inline, and
`build_reply_markup` in `client/buttons.py`, which drops button objects it
does not recognize.

`uniborg/topics.py` overrides the private `TelegramClient._dispatch_update` to
file each incoming message's private topic before any handler runs (see
[private_topics.md](private_topics.md)). Check on any new version that
`_update_loop` still hands every update to `self._dispatch_update`, and that
`send_message` and `send_file` still end in `await self(request)` with an
`InputReplyToMessage`. A test in `tests/test_topics.py` fails if the first
stops being true.

### What they do not fix

- An unknown object in a pushed message outside a container is still dropped
  unacknowledged. It is only counted.
- Skipping a difference window loses the updates in it. That is why every
  skip alerts.
- The qts re-dispatch only covers the types on its allow-list. Account `pts`
  updates (edits, deletions, read receipts) that `getDifference` recovers are
  still dropped, deliberately: re-dispatching them would change what every
  plugin sees after a reconnect. Channel differences have the same flaw
  (`apply_channel_difference`), which guest mode does not need.
- The at-most-once guard cannot tell whether an interrupted request ran. The
  caller only learns that the delivery is unknown.

## Upgrade path to 1.45.0

1. **Pin 1.43.2** in `requirements.txt` until the button refactor lands. An
   unpinned install pulls 1.45.0, which breaks at import on pre-refactor code.
   The pin is now `telethon==1.45.0`.
2. **Refactor buttons first.** 1.45.0 removes `KeyboardButtonCallback`,
   `KeyboardButtonRequestPeer` and the other per-kind keyboard classes, and
   `KeyboardButton` gains a required `type`. `uniborg/bot_util.py` imports one
   of the removed classes at startup, so without the refactor every instance
   fails to start. Build buttons through the `uniborg/tg_compat.py` helpers
   and read payloads through them too, since 1.45.0 moves callback data to
   `button.type.data` (see "Building buttons on both versions" below).
3. **Run the canary.** Give the throwaway bot its own venv (for example
   `~/.borg/canary/venv`, created with `--system-site-packages` so the other
   dependencies are shared) and install the PyPI 1.45.0 wheel there. Run the
   test suite with that interpreter, then run the canary bot on its own
   session for about a day. Watch the safety-net counts, every menu, the
   request-peer keyboard, and callback data arriving as bytes. Send `.tgcaps`
   to the canary first: it should report `telethon_version: 1.45.0`,
   `layer: 229` and `button_schema: typed`.
4. **Back up the `.session` files** before touching the shared env. Copy them
   while their instances are stopped, since SQLite may be mid-write otherwise.
5. **Upgrade the shared env** to the pinned 1.45.0. Every instance picks it up
   at its next restart, so restart them deliberately, one at a time, and watch
   the log chat.
   Stop every instance before `git pull`: plugins hot-reload when their files
   change, so a pull under running instances loads new plugin code onto the
   old core and the old Telethon still in memory. Production was upgraded
   this way on 2026-09-30: stop all, back up the sessions, pull, install
   1.45.0, start llm_chat alone and check it, then start the rest.
6. **Drop the `0x95ef6f2b` shim** once a day of unknown-constructor counts is
   clean. A layer-229 session receives `message#7600b9d3`, so the shim is dead
   code there.

Telethon 1.44.0 (layer 227) is a fallback stepping stone: it needs no button
changes and already has rich messages and guest mode, but lacks the draft stop
button and the layer-229 keyboards.

## Building buttons on both versions

Terms used here:

- **Per-kind schema**: layer 228 and older, where each button kind has its own
  generated class, such as `KeyboardButtonCallback(text, data)` or
  `KeyboardButtonRequestPeer(...)`. Telethon 1.43.2 uses it.
- **Typed schema**: layer 229, where a reply button is `KeyboardButton(text,
  type)` and an inline button is `KeyboardInlineButton(text, type)`. The
  `type` object (`ButtonTypeDefault`, `ButtonTypeRequestPeer`,
  `InlineButtonTypeCallback`, `InlineButtonTypeUrl` and so on) holds the
  payload, and inline rows become `KeyboardInlineButtonRow`. Telethon 1.45.0
  uses it. `KeyboardButtonRow`, `ReplyInlineMarkup`, `ReplyKeyboardMarkup` and
  `KeyboardButtonStyle` exist in both.

`uniborg/tg_compat.py` hides the difference. It imports only the standard
library and Telethon, so tests can load it without `uniborg.util`.

- **Build** with `callback_button(text, data)`, `url_button(text, url)`,
  `text_button(text)` and `request_peer_button(text, button_id=...,
  peer_type=..., max_quantity=1)`. Nested lists of them work as `buttons=`
  exactly as before. For a markup object (a raw request, or keyboard options
  such as `resize`), use `inline_keyboard(rows)` or `reply_keyboard(rows,
  resize=..., single_use=...)`, or `button_row(buttons)` for one row.
- **Read** with `button_data` (bytes or None), `button_data_text`,
  `button_text`, `button_url`, `request_peer_spec` and `is_callback_button`,
  never with `.data`. They also accept Telethon's `Button` and `MessageButton`
  wrappers. Annotate with `tg_compat.CallbackButton`, never the removed
  classes.
- **Callback data** keeps the old meaning: a str is sent as its UTF-8 bytes
  and bytes are sent unchanged. Telegram accepts 1 to 64 bytes, so anything
  else raises `ValueError` when the button is built, not when the message is
  sent. Unlike `Button.inline`, empty data is an error rather than a copy of
  the button text.
- **Option menus** (one button per choice, the current one ticked) come from
  `bot_util.option_buttons(options, current_value=..., callback_prefix=...)`,
  which `present_options`, `tts_bot` and `image_gen` share. `llm_chat` calls
  `callback_button` directly.
- **Tests** read buttons through the same readers. Never compare `.data` or
  check `isinstance` against a per-kind class: both break on one of the two
  versions.

Why the builders return generated TL objects: 1.45.0's `build_reply_markup`
keeps only real `KeyboardButton` and `KeyboardInlineButton` instances and
silently drops anything else, so a look-alike object sends the message without
its keyboard and logs nothing. 1.43.2 raised `AttributeError` on the same
object.

The builders attach no style object unless asked. On 1.43.2
`callback_button(text, data)` therefore serializes to the same bytes as the
old `KeyboardButtonCallback(text, data)`, whereas `Button.inline` always adds
an empty `KeyboardButtonStyle`.

**Button styles.** Every builder takes `style=ButtonStyle.PRIMARY` (blue),
`SUCCESS` (green) or `DANGER` (red), or the same names as strings. An unknown
style raises `ValueError`. Both pinned versions support styles. On a Telethon
without `KeyboardButtonStyle`, the builders build the button without its
style. That is not a silent fallback for an unknown value: the style is
validated first on every version, and a style only colours a button; it never
changes what the button sends.

## Rollback

After the button refactor the code runs on both versions, so rolling back is
running `pip install telethon==1.43.2` in the shared env and restarting. The
tests pass on 1.43.2 too, so the pin in `requirements.txt` can stay at 1.45.0
for a short rollback. The session
format is the same in both versions, and the next connect declares layer 224
again. Restore a `.session` backup only if a session was damaged, and never
run a backup and its live copy at the same time.

If a safety net itself misbehaves, set `borg_tg_safety_nets=0` for that
instance and restart. Telethon's own behaviour comes back unchanged.

## Never do

- **Never run a Bot API sidecar with a production token.** The first HTTP call
  starts a cloud Bot API instance, which is a second authorization of the bot.
  Telegram gives "no guarantee" of update delivery with two logins, and undoing
  it needs `logOut`, which then blocks cloud logins for 10 minutes.
- **Never hand-write layer-229 TL on 1.43.2.** Thirty-two core constructors
  changed ids between 224 and 229, replies come back in the newer layer, and
  covering what arrives means re-deriving 1.45.0. One parsing mistake corrupts
  everything after it in the same buffer.
- **Never wrap individual requests in `InvokeWithLayer`.** It switches the
  layer saved for the connection, and the older parser then faces newer
  objects on every later request.
- **Never override `LAYER`.** Declaring 229 with layer-224 generated classes
  asks the server for objects Telethon cannot parse.
- **Never install Telethon from git HEAD.** The v1 branch after 1.45.0 was
  briefly unimportable. Install the PyPI wheel.
- **Never run two processes on one session file.** Opening more concurrent
  sessions than the server's `tmp_sessions` (default 1) makes it terminate all
  of them with `AUTH_KEY_DUPLICATED` and invalidate the key. For a bot, logging
  back in needs the token. This includes pointing the canary venv at a
  production session.
- **Never enable BotFather Guest Mode before 1.45.0 runs on that instance.** On
  1.43.2 either the mentions never arrive, or the `updateBotGuestChatQuery`
  update is dropped and the difference that tries to recover it fails too. The
  difference fallback now skips that window instead of disconnecting, but the
  mentions are still lost.

## Checking what an instance can use: `.tgcaps`

`.tgcaps` is an admin-only command in `uniborg/_core.py`, so every instance has
it. Only the logged-in account itself and the admins in `uniborg/util.py` can
use it. It replies with one `name: value` line per capability, then a
`safety_nets:` line holding `borg.safety_stats.summary()` when the safety nets
were installed. Each call makes one fresh `get_me()`, because BotFather
settings such as Guest Mode change without a restart.

There are two kinds of capability:

- **Schema flags** say which generated TL types this Telethon has. A true flag
  means the request can be built, not that Telegram accepts it for this
  account or chat.
  - `telethon_version`, `layer` and `button_schema` (`per_kind` or `typed`).
  - `button_styles`: `KeyboardButtonStyle` exists.
  - `draft_text`: the draft action exists (layer 224 has the old form).
  - `draft_stop`: the draft action takes `can_stop` and
    `SendMessageStopDraftAction` exists, so users can stop a live draft.
  - `rich_messages`: `InputRichMessageMarkdown` exists and sends, edits and
    inline edits all take `rich_message`. `rich_drafts` is the rich draft
    action.
  - `guest_types`: `UpdateBotGuestChatQuery` and
    `SetBotGuestChatResultRequest` exist.
  - `ephemeral` and `formatted_dates` (the Bot API `date_time` entity).
- **Account flags** come from `get_me()`.
  - `is_bot`.
  - `guest_enabled`: BotFather Guest Mode is on (`bot_guestchat`). It reads
    `unknown` on 1.43.2, whose `User` has no such field.
  - `private_topics` (`bot_forum_view`), `business` (`bot_business`) and
    `inline_mode` (an inline placeholder is set).

In code, `await tg_compat.capabilities_of(borg)` probes once and caches the
result on the client, `refresh=True` probes again, and
`tg_compat.cached_capabilities(borg)` returns the cached value (or None)
without awaiting. Gate new paths on these flags, not on a version number.

## Related files

- `uniborg/telethon_safety.py`: the safety nets.
- `uniborg/uniborg.py`: installs them in `Uniborg.create` and sends alerts.
- `uniborg/_core.py`: the `.tgcaps` command.
- `uniborg/telethon_compat.py`: the `0x95ef6f2b` shim.
- `uniborg/tg_compat.py`: the button helpers and `TgCapabilities`.
- `tests/test_telethon_safety.py`: synthesized containers and fallback paths.
  Run it under both versions:
  `python3 -m pytest tests/test_telethon_safety.py` and the same command with
  the canary venv's interpreter.
- `tests/test_tg_compat.py`: every button helper and capability flag,
  including `build_reply_markup` on a never-connected client. Run it under
  both versions too.

To check a new version against a server's own packages without touching its
shared env, install the wheel into a `python -m venv --system-site-packages`
venv and run the suite with that interpreter. Run it in a tmux pane, and pass
`--foreground` if you wrap it in `timeout`: importing `uniborg.util` starts a
Brish zsh server, and a plain `timeout` runs the suite in a background process
group, so that server is stopped by SIGTTIN and collection hangs silently.
