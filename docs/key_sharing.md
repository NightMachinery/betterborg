# Key Sharing and Key Order

A user can let another user's requests run on one of their own API keys without
revealing the key. Each user also chooses the order in which the keys open to
them are tried.

Code: `uniborg/key_sharing.py` (shares, order, the admin share, failure
classification) and the `/keys` panels and request path in
`llm_chat_plugins/llm_chat.py` (`_credential_chain`, `_request_credentials`,
`_CredentialFailed`).

## Credential chain

For each service (OpenAI, Gemini, OpenRouter, …), a user's **credential chain**
is the list of keys their requests may use:

- their own key, if they set one;
- keys other users share with them, oldest share first;
- the admin share, for OpenAI (below).

A request uses the first key. When that key is rejected (401/403), out of
credit (402, or OpenAI's `insufficient_quota`) or rate limited (429), and
nothing has been streamed yet, the same request is retried on the next key and
the answer carries a note naming both keys. Other errors are reported as before.
A failure on the last key is reported normally.

The chain leaves out sources the user turned off, owners who have no key at the
moment, and a key that already appeared earlier (an owner who shares the very
key you hold).

OpenAI keys that `openai_providers.QuotaMemory` remembers as out of credit move
to the end of the chain for 72 hours ([openai_provider.md](openai_provider.md)).
The provider decision looks at every key in the chain, so a user whose own key
is spent but who holds a working shared key stays on the OpenAI API.

For native Gemini models in files mode, a retry on another key rebuilds the
history, because uploaded files belong to the key that uploaded them.

## Sharing

`/shareKey <service> <user-id or @username>` asks for confirmation, then shares
the owner's own key for that service. Only one's own keys can be shared, not
keys shared with you. The share always uses the owner's current key: setting a
new key updates every share, and deleting it pauses them. The recipient is told
when a share starts and ends, if they have started the bot. The start message
says that requests on the key run on the owner's account (whose provider logs,
if any, the owner can read) and has a **Don't use this key** button, which
turns the share off in the recipient's chain.

Requests made with a shared key are billed to its owner.

## /keys

`/keys` shows, per service, the chain in order (keys themselves are never
shown), and the keys you share, each with a button to stop sharing. **Order**
opens a panel where ⬆/⬇ move a source and ✅/🚫 turn it on or off. The buttons
name the source, not its position, so a list that changed since the panel was
drawn is refused rather than misapplied. A source not in your saved order (a
share made later) goes after the saved ones. Viewing the panel does not
advance Gemini key rotation.

A bot admin opens any user's panel with `.keys <user-id>`, and can reorder,
toggle and revoke there as the user could. The admin view also sets the user's
OpenAI and Gemini providers.

## Admin share

A bot admin can share their OpenAI key with every Codex user (the people their
Codex subscription is shared with): **Share my OpenAI key** on the
`.codex-users` list. It is off until switched on, needs the admin's own OpenAI
key, and covers one admin at a time (a second admin switching it on takes it
over). Each user's detail panel has an override, **OpenAI share**: follow Codex
access (the default), always on, or always off. The share appears in the chain
as "Admin share by …", after the user's own and shared keys unless they reorder
it. It pays only for the GPT families (GPT Sol, Astra and Luna on the OpenAI
API), not for other model ids a user can type under `openai/`.

Switched on, it moves Codex users on the default provider `openai` from Codex
to the admin's key; when the key runs out of credit, their messages fall back
to Codex ([openai_provider.md](openai_provider.md)).

The request path decides Codex access per message, so trusted chats count. The
`/keys` display only knows the personal Codex grant from the roster, so a user
who has Codex only through a trusted chat does not see the admin share listed,
though their requests in that chat use it.

## Usage

Code: `uniborg/key_usage.py`.

Every answered request that someone else pays for adds to a daily row of the
`key_usage_daily` table: Codex requests (paid by the shared subscription) and
requests made with another user's key (paid by its owner). Requests on a
user's own key are not recorded.

- `/keys` shows the owner who else used their keys in the last 30 days, per
  user and service.
- The `.codex-users` detail panel shows the admin a user's last 30 days on
  Codex and on others' keys.

Codex and the OpenAI API report tokens, so their rows carry token counts and an
estimated cost. Other services (Gemini, OpenRouter, …) record request counts
only; a total that includes them ends in `+`, since its cost is a lower bound.
Costs use approximate list prices per GPT family (`key_usage.PRICES`), with
cached input assumed at a tenth of the input price. Codex requests are priced
as if they had run on the API, to show what the subscription covers. Treat all
of it as an estimate, not a bill.
