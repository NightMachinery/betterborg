# Telethon Upgrades and Telegram Layers

How this repo copes with Telegram's schema versions, what protects a running
bot from objects Telethon cannot parse, and how to move from Telethon 1.43.2
to 1.45.0 without taking every instance down.

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

Every event is logged as a warning starting with `Telegram safety net`, counted
per kind and per constructor in `borg.safety_stats` (a `SafetyStats`, whose
`summary()` gives one line), and alerted to the log chat. Alerts go out at
most once per kind every 10 minutes, and the next one says how many events
were held back. With no log chat the event is only logged, and a failed alert
send is logged too.

### Switching them off

`borg_tg_safety_nets` controls all of them. It is on when unset. `0`, `false`,
`no` or `off` turns every net off, and `1`, `true`, `yes` or `on` keeps them
on. Any other value stops startup with a `ValueError`, so a typo cannot
silently disable them.

### Version guard

The container skip and the message guard replace private Telethon code, so
they install only on the versions they were checked against: 1.43.2 and
1.45.0. On any other version they are skipped with a warning. The log counter
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
  `ChannelPrivateError` the channel fallback relies on.

### What they do not fix

- An unknown object in a pushed message outside a container is still dropped
  unacknowledged. It is only counted.
- Skipping a difference window loses the updates in it. That is why every
  skip alerts.
- Telethon's `MessageBox` drops `pts`/`qts` updates that `getDifference`
  recovered after a gap. That is a separate problem, planned as its own patch.

## Upgrade path to 1.45.0

1. **Pin 1.43.2** in `requirements.txt`. This is done. An unpinned install
   pulls 1.45.0, which breaks at import.
2. **Refactor buttons first.** 1.45.0 removes `KeyboardButtonCallback`,
   `KeyboardButtonRequestPeer` and the other per-kind keyboard classes, and
   `KeyboardButton` gains a required `type`. `uniborg/bot_util.py` imports one
   of the removed classes at startup, so without the refactor every instance
   fails to start. Build buttons through version-neutral helpers and read
   payloads through a helper, since 1.45.0 moves callback data to
   `button.type.data`.
3. **Run the canary.** Give the throwaway bot its own venv (for example
   `~/.borg/canary/venv`, created with `--system-site-packages` so the other
   dependencies are shared) and install the PyPI 1.45.0 wheel there. Run the
   test suite with that interpreter, then run the canary bot on its own
   session for about a day. Watch the safety-net counts, every menu, the
   request-peer keyboard, and callback data arriving as bytes.
4. **Back up the `.session` files** before touching the shared env. Copy them
   while their instances are stopped, since SQLite may be mid-write otherwise.
5. **Upgrade the shared env** to the pinned 1.45.0. Every instance picks it up
   at its next restart, so restart them deliberately, one at a time, and watch
   the log chat.
6. **Drop the `0x95ef6f2b` shim** once a day of unknown-constructor counts is
   clean. A layer-229 session receives `message#7600b9d3`, so the shim is dead
   code there.

Telethon 1.44.0 (layer 227) is a fallback stepping stone: it needs no button
changes and already has rich messages and guest mode, but lacks the draft stop
button and the layer-229 keyboards.

## Rollback

After the button refactor the code runs on both versions, so rolling back is
installing the 1.43.2 pin into the shared env and restarting. The session
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

## Related files

- `uniborg/telethon_safety.py`: the safety nets.
- `uniborg/uniborg.py`: installs them in `Uniborg.create` and sends alerts.
- `uniborg/telethon_compat.py`: the `0x95ef6f2b` shim.
- `tests/test_telethon_safety.py`: synthesized containers and fallback paths.
  Run it under both versions:
  `python3 -m pytest tests/test_telethon_safety.py` and the same command with
  the canary venv's interpreter.
