# Gemini Context Caching & Free-Tier Fallback

How native Gemini context caching is applied, and how the bot recovers when a key cannot
use caching.

## Summary

- For native Gemini models (`model.startswith("gemini/")`), the chat handler adds
  `cache_control: {"type": "ephemeral"}` to every message so litellm enables full
  context caching.
- **Free-tier API keys have a per-model cached-content storage limit of 0.** Requests with
  caching then fail with a 429:
  `TotalCachedContentStorageTokensPerModelFreeTier limit exceeded ... limit=0`.
- When that specific 429 is hit, the bot **disables caching for that (key, model) pair** and
  **silently retries the same request without caching**, so the user still gets a reply. No
  error is shown for the recoverable case.
- Future requests for that (key, model) skip caching pre-emptively, so the 429 does not recur.

## Scope: per (key-hash, model), global

The disabled flag is keyed by `sha256(api_key)[:32]` + model name, stored globally in Redis —
**not** per user. This is correct under:
- **key rotation** (`~/.gemini_api_keys`): each physical key is tracked independently, so a
  paid key is not penalized because a free key in the pool hit the limit;
- **shared keys**: the same key used by multiple users shares one flag.

The raw key is never stored — only its hash.

## Lifetime

The flag has a **30-day TTL** (`GEMINI_CACHE_DISABLED_DURATION` in `uniborg/history_util.py`).
The free-tier limit is effectively permanent, so the TTL is just a periodic **re-probe**: after
30 days the bot tries caching again once, in case the key was upgraded to a paid tier. If it is
still free-tier, the next request re-detects and re-disables (one recoverable 429).

## Code map

- `uniborg/redis_util.py` — `gemini_cache_disabled_key(key_hash, model)`: the Redis key.
- `uniborg/history_util.py`:
  - `is_gemini_caching_disabled(api_key, model)` — read the flag (returns `False` when Redis
    is unavailable, so caching is attempted and the worst case is a recoverable one-time 429).
  - `disable_gemini_caching(api_key, model)` — set the flag with the 30-day TTL.
- `llm_chat_plugins/llm_chat.py`:
  - `_apply_cache_control` / `_strip_cache_control` — add/remove `cache_control` on all messages.
  - `is_cache_storage_quota_error(exception)` — detects the cache-storage 429 (matches
    `CachedContentStorageTokens` in the error body), distinguishing it from an ordinary rate
    limit so only the recoverable case triggers the silent retry.
  - `chat_handler` — applies caching unless disabled; on the cache-storage 429 it disables
    the flag, strips `cache_control`, and retries the request once.

## Behavior when Redis is down

If Redis is unavailable, `is_gemini_caching_disabled` returns `False` (caching is attempted)
and `disable_gemini_caching` is a no-op. A free-tier key then incurs the recoverable one-time
429 + silent retry on **each** request (the flag can't persist), but requests still succeed.
