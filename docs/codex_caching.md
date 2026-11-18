# Codex Prompt Caching

Betterborg's Codex-backed chat uses the OpenAI Responses API through
`uniborg/codex_util.py`.

OpenAI's Prompt Caching is automatic for recent models, including Codex-family
models listed in OpenAI's prompt-caching guide. Cache hits require an exact
prompt prefix match and prompts of at least 1024 tokens. The Responses API
reports cache hits in `usage.input_tokens_details.cached_tokens`. Betterborg
uses that count for [usage accounting](key_sharing.md#usage) and does not show
it in individual replies.

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

## Sign in with ChatGPT preview

The [plan-usage preview](https://developers.openai.com/siwc/token-sharing-open-source/preview-limitations)
requires the needed history on each HTTP request and rejects
`prompt_cache_retention`. A [public test of the preview](https://community.openai.com/t/sign-in-with-chatgpt-configuration-update-rejected-so-changing-reasoning-effort-rebuilds-the-prompt-cache/1402231)
observed 8,960 cached tokens in a roughly 9,900-token prompt when it reused the
same reasoning effort. The first request at a different effort missed.

For a hosted Betterborg rollout, verify cache hits with representative
multi-turn chats and check how both cold and warm requests draw down the
account's [app usage](https://help.openai.com/en/articles/20001542-using-your-chatgpt-plan-in-other-apps-and-sites).
Cached-token counts confirm reuse. The public preview docs do not specify the
discount against included plan usage. Keep a history budget that controls
input size on cold requests too.

## Non-goals

- Betterborg does not implement its own Codex prompt cache.
- Betterborg does not use Gemini-style `cache_control` for Codex.
