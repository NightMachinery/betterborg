# Codex Prompt Caching

Betterborg's Codex-backed chat uses the OpenAI Responses API through
`uniborg/codex_util.py`.

OpenAI's Prompt Caching is automatic for recent models, including Codex-family
models listed in OpenAI's prompt-caching guide. Cache hits require an exact
prompt prefix match and prompts of at least 1024 tokens. OpenAI reports cache
hits in `usage.prompt_tokens_details.cached_tokens`, though Betterborg does not
currently surface those counters in chat logs or replies.

Source checked: `https://developers.openai.com/api/docs/guides/prompt-caching.md`.

## Betterborg request shape

Codex requests use:

- `store: false` — disables provider-side response persistence; it is not a
  cache opt-out.
- `prompt_cache_key` — a deterministic, non-secret hash scoped to Betterborg,
  Codex, model, and chat, to improve OpenAI cache routing. It intentionally
  does not include the Telegram user ID.

Betterborg does not send `prompt_cache_retention` for Codex/ChatGPT-auth calls
because that endpoint rejects the parameter.

Betterborg still sends the full selected conversation context on each request;
OpenAI decides which repeated prefix is served from cache.

## Prefix stability

Provider-side automatic caching is prefix based, so Betterborg keeps volatile
runtime facts out of the system prompt. The system prompt remains stable across
requests, while current date/time is appended to the latest user turn as runtime
context.

This improves cache friendliness for Codex and other automatic-cache providers
without provider-specific cache-control markup.

## Non-goals

- Betterborg does not implement its own Codex prompt cache.
- Betterborg does not use Gemini-style `cache_control` for Codex.
- Betterborg does not currently display Codex `cached_tokens` usage.
