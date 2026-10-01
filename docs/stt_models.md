# STT Models

Which Gemini model the STT bot (`stt_plugins/stt.py`) transcribes with, and
how it falls back. The list is `STT_MODELS` in `uniborg/constants.py`:

1. `gemini/gemini-2.5-flash`, the default.
2. `gemini/gemini-flash-lite-latest`, Google's alias for its newest Flash Lite.

The call runs with the caller's own Gemini key (or a rotated shared key), so
whether a model works can depend on the key.

## Transient errors

A rate limit, overload or timeout (`_is_retriable_stt_error`) is retried up to
`STT_RETRIES_PER_MODEL` times on each model, `STT_RETRY_SLEEP` seconds apart,
before the next model in the list. The status message says so as it happens.

## A model that refuses the key

Google closed Gemini 2.5 Flash to new API keys: for those it answers 404
"This model models/gemini-2.5-flash is no longer available to new users".
Retrying cannot help, so such a refusal (`_is_model_unavailable_error`) moves
to the next model at once, and the bot remembers it for that key:

- Redis key `borg:model_unavailable:<key hash>:<model>`, where the hash is the
  first 32 hex digits of the key's SHA-256 (`redis_util.api_key_hash`), never
  the key itself.
- It lasts `STT_MODEL_UNAVAILABLE_SECONDS` (30 days), and while it lasts that
  key skips the model without a call. Other keys still try it.
- When every model in the list has refused the key, all are tried again, so a
  stale mark cannot leave a key with nothing.

So keys that can still use 2.5 Flash keep it, and new keys use Flash Lite
Latest. When Google shuts 2.5 Flash off for everyone, every key falls through
the same way.
