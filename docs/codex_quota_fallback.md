# Codex Quota Fallback

When Codex cannot answer at all, `llm_chat` shows a structured panel instead of
the raw provider error, and offers a temporary stand-in model.

This is the last resort. The first response to a spent allowance is the
automatic retry on the Luna Reserve; see
[Codex Luna Reserve](codex_luna_reserve.md). The panel appears only when the
Reserve is unavailable or also spent.

## The error being handled

The backend answers an exhausted plan with HTTP 429 and a body naming the
limit, the plan, and when it resets. Previously the whole exception was
stringified into the reply, producing a wall of raw Python dict text.

`codex_util.parse_usage_limit` now reads it into a `CodexUsageLimit`, which
`CodexStreamError` carries. It accepts the SDK's decoded `body`, a raw mapping,
a streaming `error` event, or the `Error code: N - {...}` exception message.
That message interpolates the body with `repr`, so it is a Python literal with
single quotes and `None` rather than JSON, and is read with `ast.literal_eval`.

Only `usage_limit_reached` counts. An ordinary rate limit is a different thing
and must not arm a multi-day fallback.

For the reset time, an absolute `resets_at` is preferred over
`resets_in_seconds`, because the relative value is computed when the error is
minted and drifts by however long the error took to arrive. Each is validated
on its own, so an implausible absolute value degrades to the relative one
rather than losing the deadline. Deadlines in the past, or beyond a sane plan
window, are discarded.

## What the panel shows

- Which allowances are spent, with percentage used and reset times in local
  time and as a relative duration.
- The plan type, when reported.
- The Luna Reserve meter, but only when the account actually has one.
- The stand-in models the user holds a usable API key for, as buttons.
- For the rest, the command that would configure them.

When no stand-in is reachable the panel says so and names `/setGeminiKey` and
`/setOpenRouterKey` rather than offering a button that cannot work.

## What the stand-in does

While armed, requests that would have used a Codex model use the stand-in
instead. Specifically:

- It is per-user. The quota is account-wide, but arming a stand-in changes
  nothing for anyone else. In a shared group one member can be redirected while
  another still sees the panel.
- It applies to saved defaults only: the personal model and the chat model.
- It never applies to per-message prefixes. `.c`, `.ch`, `.cxx`, `.as`, `.cr`,
  `.i` and their Persian aliases are deliberate Codex requests and keep
  reporting the limit themselves.
- It never rewrites saved settings. The saved Codex model returns untouched.
- Replies carry a one-line note naming the model that actually answered. That
  note rides the existing warning footer, so it appears in private chats and is
  stripped in groups.

## How it ends

- Automatically, at its deadline. Expiry is checked when the record is read, so
  it survives restarts and leaves no timer behind. A request already in flight
  keeps the model it started with.
- The "Switch back to Codex now" button on the panel.
- Any explicit model choice: `/setModel`, `/setModelHere`, their menus and
  text flows, and an admin assigning a default through `.codex-users`.
- Automatically, if the stand-in's own API key stops working. Rather than
  prompting for a key the user never chose, the stand-in is dropped and the
  reason explained.

## The command

`/codexStatus`, also reachable as `.codex-status`, opens the panel on demand
and reports the account's live meters rather than guessing from the last
failure. `/status` grows a stand-in line only while one is armed.

The command is not private-only: a stand-in armed in a group has to be
cancellable there. An armed stand-in opens the panel even for a user who has
since lost Codex access, or they could never cancel it.

## Panel buttons

The owner's user id and the window deadline travel inside the callback payload,
so both checks work after a restart with no stored record of the panel.
Pressing someone else's panel, an expired window, an unknown model token, and a
stand-in whose key has since been removed are each refused without a write.

Userbots get no inline buttons. `/codexStatus` falls back to the numbered-menu
flow, which shares one mutation with the button callback so the two cannot
drift. The panel is deliberately not turned into an input flow inline in the
error path, where it would collide with any pending input and swallow the
user's next message.

## Related

- [Codex Luna Reserve](codex_luna_reserve.md)
- [Codex models](codex_models.md)
