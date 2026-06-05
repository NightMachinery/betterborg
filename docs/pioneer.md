# Pioneer Backend

Betterborg supports Pioneer through Pioneer's OpenAI-compatible endpoint:
`https://api.pioneer.ai/v1`.

Sources checked:

- `https://docs.pioneer.ai/introduction`
- `https://docs.pioneer.ai/api-reference/inference/openai-compatible.md`
- `https://docs.pioneer.ai/api-reference/coding-agent-integration.md`
- `https://docs.pioneer.ai/api-reference/prompt-caching.md`

## Models

The Pioneer models are shown in chat model menus only for admins:

- `pioneer/claude-opus-4-8` — Pioneer Opus 4.8
- `pioneer/gpt-5.5` — Pioneer GPT-5.5
- `pioneer/claude-sonnet-4-6` — Pioneer Sonnet 4.6

They are sent through Pioneer's OpenAI-compatible Responses API directly with
the OpenAI Python client pointed at `https://api.pioneer.ai/v1`. Betterborg
bypasses LiteLLM for Pioneer chat responses because LiteLLM's Responses bridge
can emit Pydantic serializer warnings while replaying streamed response objects.

## API keys

Admins can set the Pioneer key with `/setpioneerkey`. The key is stored using the
same per-user API key storage used by the other chat backends.

## Tools

Pioneer docs describe OpenAI-compatible chat/completions and Responses endpoints.
Betterborg uses the Responses endpoint and maps the existing `googleSearch`
toggle to the OpenAI-compatible built-in tool `{"type": "web_search"}` for
Pioneer requests.

`urlContext` and `codeExecution` are not mapped for Pioneer.

## Reasoning, storage, and caching

Pioneer reasoning effort defaults to `medium`. A prefix-selected reasoning effort
overrides `/setthink`; otherwise `/setthink` is used unless it is `disable`.
Betterborg sends it to the Responses API as `reasoning={"effort": ...}`.

Betterborg sends `store: false` on Pioneer Responses calls so Pioneer does not
persist the request/response payload for adaptive training or evaluation.

Betterborg does not add its own `cache_control` or OpenAI cache-specific params
to Pioneer requests. It does keep volatile runtime facts out of the system prompt
and appends them to the latest user turn, preserving a stable prefix for
Pioneer's automatic prompt caching. Pioneer docs state that GPT-family models
cache prompt prefixes upstream, and Claude/Opus models get Pioneer-inserted
cache breakpoints when the prompt is large enough.

## Attachments

For Pioneer Responses calls, Betterborg forwards text, image attachments, and PDF
file data. Assistant history is preserved as text only. Unsupported binary media
parts are skipped before the request is sent.
