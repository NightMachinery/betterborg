# Codex Models

`llm_chat` exposes the ChatGPT Codex backend as access-controlled models:

- `openai-codex/gpt-5.6-sol`
- `openai-codex/gpt-6-astra`

Users without Codex access do not see them in `/setModel` or `/setModelHere`,
and direct selection attempts are rejected server-side.

## Access configuration

On first use, Betterborg creates `~/.borg/llm_chat_config.json5` if it does not
exist. Set `LLM_CHAT_CONFIG_PATH` to use another path. The default is:

```json5
{
  codex_allowed_users: ["MAGIC_ADMINS"],
  codex_imagegen_allowed_users: ["MAGIC_ADMINS"],
}
```

Each array is a complete policy. Entries may be numeric Telegram user IDs or
the exact string `"MAGIC_ADMINS"`. The sentinel delegates to Betterborg's
existing `util.isAdmin(event)` check, including trusted-chat access. A numeric
ID grants only the named Codex capability; it does not make that user a bot
admin or permit changing group settings. Remove the sentinel to remove
automatic admin access. An empty array grants nobody access.

Both keys are required. Betterborg reloads the file when it changes. If the
file is invalid or unreadable, all Codex access is disabled until it is fixed;
the invalid file is logged and left untouched. Other model providers continue
working. Image generation requires a user to pass both policies.

Runtime requirements:

- Install `requirements.txt` in the same Python environment that runs
  `stdborg.py`.
- Run `codex login` for that runtime user so `~/.codex/auth.json` contains
  ChatGPT OAuth credentials.

The integration depends on the published `llm-openai-via-codex` pip package for
Codex OAuth token borrowing and refresh. Betterborg only owns the Telegram
message conversion, access checks, and streaming response handling.

## Availability

Verified directly against the ChatGPT Codex backend, the models exposed to a
ChatGPT account are `gpt-5.6-sol`, `gpt-5.6-terra`, `gpt-5.6-luna`, `gpt-5.5`,
`gpt-5.4` and `gpt-5.4-mini`.

GPT-6 Astra is wired up but is still rolling out. Until it reaches the account,
a request returns:

    The 'gpt-6-astra' model is not supported when using Codex with a ChatGPT account.

No code change is needed once it goes live. `OPENAI_CODEX_LATEST` in
`uniborg/constants.py` can then be pointed at `OPENAI_CODEX_ASTRA`.

## Reasoning effort

Reasoning effort is a per-model preference. See `docs/reasoning_effort.md`.

The Responses API accepts `none`, `minimal`, `low`, `medium`, `high`, `xhigh`
and `max`. GPT-5.6 Sol exposes all of these except `minimal` in the bot menus;
GPT-6 Astra drops `none` as well; GPT-5.5 has no `max`.

The `ultra` level advertised by the Codex CLI model list is a Codex-app
subagent mode, not an API value. Sending it returns an "Invalid value" error,
so it is deliberately absent from the level sets.

## Access-controlled quick prefixes

- `.c` and `.cm`: GPT-5.6 Sol with `medium` reasoning.
- `.cl`: GPT-5.6 Sol with `low` reasoning.
- `.ch`: GPT-5.6 Sol with `high` reasoning.
- `.cx`: GPT-5.6 Sol with `xhigh` reasoning.
- `.cxx`: GPT-5.6 Sol with `max` reasoning.
- `.as`, `.asm`, `.asl`, `.ash`, `.asx`, `.asxx`: the same ladder for GPT-6
  Astra.

`.a` belongs to `advanced_get` and `.o` was Pioneer's, so Astra uses `.as`.

## Tools

The `googleSearch` toggle maps to the OpenAI Responses `web_search` tool for
Codex models. Other Gemini-specific tools are not mapped.

## Attachment handling

Codex requests are sent through the OpenAI Responses API. Betterborg only
forwards image attachments as `input_image` parts for Codex models; non-image
binary attachments such as videos, audio, PDFs, stickers, and unknown MIME types
are skipped before the request is sent. Inline data URLs are validated to ensure
they use an `image/*` MIME type.

## Prompt caching

See [docs/codex_caching.md](codex_caching.md) for Codex prompt caching behavior.

## Image generation

Documentation, code, and a live OAuth request verified on 2026-09-12:

- Codex supports built-in image generation with ChatGPT subscription access.
  The [official image-generation guide](https://learn.chatgpt.com/docs/image-generation)
  says it uses `gpt-image-2`, counts toward general Codex usage limits, and
  consumes included limits about 3–5 times faster than comparable text turns,
  depending on image quality and size.
- Betterborg's Codex adapter currently supports image inputs and text outputs.
  The chat integration only enables `web_search`; it does not request an
  `image_generation` tool. `stream_codex_response()` consumes text deltas and
  completion status, without extracting generated images.
- The separate `image_gen_plugins/image_gen.py` plugin uses Google Imagen;
  it is not connected to Codex OAuth.

### Live verification

The borrowed-token approach works for image generation on the tested account.
A standalone probe loaded the actual `_create_async_client()` and
`prepare_codex_response_kwargs()` functions from `uniborg/codex_util.py`, omitting
the application startup import. It used the installed `llm-openai-via-codex`
authentication helper and OpenAI Python SDK 2.37.0, with no API-key fallback or
additional Codex client headers.

The request went to `https://chatgpt.com/backend-api/codex/responses` with
`gpt-5.6-sol`, `reasoning.effort="low"`, `store=false`, `stream=true`, and:

```json
{"tools": [{"type": "image_generation"}]}
```

The instructions explicitly requested one image using the tool. The prompt was
an orange circle centered on a white square background. The response:

- Selected `gpt-image-2-codex`, PNG output, and automatic quality and size.
- Emitted image-generation progress events and a `response.output_item.done`
  item with `type="image_generation_call"` and base64 image data in `result`.
- Returned one valid 1254 by 1254 PNG, 724,526 bytes, in about 19.8 seconds.
  Pillow verified the file, and visual inspection confirmed the requested image.
- Finished with `response.completed` and status `completed`.

This proves endpoint compatibility for a basic generation, not end-to-end
Telegram support. Betterborg still needs to enable the tool, collect/decode
image items, and send them to Telegram. Deduplicate image items by ID if handling
both `response.output_item.done` and `response.completed`. This request emitted
`response.output_text.done` without text delta events, so image delivery must not
depend on the current text accumulator.

The same endpoint and event format are documented by the
[chatgpt-imagegen project](https://github.com/leeguooooo/chatgpt-imagegen/blob/main/docs/how-it-works.md).
A [Codex issue](https://github.com/openai/codex/issues/28723) reports that explicit
size and quality parameters can be overridden. Our test used defaults; edits,
explicit image model selection, dimensions, quality controls, and other account
entitlements remain untested.

Reusing OAuth avoids a separate API-key integration and uses the subscription
path, but the internal endpoint and parameter behavior can change. The
[OpenAI image-generation API](https://developers.openai.com/api/docs/guides/tools-image-generation)
offers the documented public API contract with separate API billing.
