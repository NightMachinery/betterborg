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
Codex models. The current-message `.i` prefix adds `image_generation` with
`partial_images: 3` when both access policies allow it. There is no persistent
image-generation toggle. Other Gemini-specific tools are not mapped.

## Attachment handling

Codex requests are sent through the OpenAI Responses API. Betterborg only
forwards image attachments as `input_image` parts for Codex models; non-image
binary attachments such as videos, audio, PDFs, stickers, and unknown MIME types
are skipped before the request is sent. Inline data URLs are validated to ensure
they use an `image/*` MIME type.

## Prompt caching

See [docs/codex_caching.md](codex_caching.md) for Codex prompt caching behavior.

## Image generation

Use `.i` to enable image generation for a single request. It combines with
model and reasoning prefixes, such as `.i .cl draw a fox`. Both access policies
must allow the sender. An explicit Codex prefix takes precedence, otherwise the
selected Codex model is used, falling back to GPT-5.6 Sol when the selected
model is from another provider. Explicit non-Codex prefixes conflict with `.i`.

Every streamed preview and completed image is sent separately to Telegram,
replying to the request. Previews remain after completion. Text responses and
clarification questions continue to work, and image attachments remain
available for editing prompts. Historical `.i` messages and replies to generated
images do not enable the tool for the next request.

See [Codex image generation](codex_image_generation.md) for stream handling,
failure behavior, and the live OAuth verification with previews enabled.
The separate `image_gen_plugins/image_gen.py` plugin continues to use Google
Imagen.
