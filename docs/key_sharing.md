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
when a share starts and ends, if they have started the bot.

Requests made with a shared key are billed to its owner.

## /keys

`/keys` shows, per service, the chain in order (keys themselves are never
shown), and the keys you share, each with a button to stop sharing. **Order**
opens a panel where ⬆/⬇ move a source and ✅/🚫 turn it on or off. A source not
in your saved order (a share made later) goes after the saved ones.

A bot admin opens any user's panel with `.keys <user-id>`, and can reorder,
toggle and revoke there as the user could. The admin view also sets the user's
OpenAI and Gemini providers.

## Admin share

A bot admin can share their OpenAI key with every Codex user (the people their
Codex subscription is shared with). It is off until switched on, and has a
per-user override: follow Codex access, always on, or always off. It appears in
the chain as "Admin share by …".

The request path decides Codex access per message, so trusted chats count. The
`/keys` display only knows the personal Codex grant from the roster, so a user
who has Codex only through a trusted chat does not see the admin share listed,
though their requests in that chat use it.
