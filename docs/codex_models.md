# Admin-Only Codex Models

`llm_chat` exposes the ChatGPT Codex backend as admin-only models:

- `openai-codex/gpt-5.6-sol`
- `openai-codex/gpt-6-astra`

Non-admin users should not see them in `/setModel` or `/setModelHere`, and
direct selection attempts are rejected server-side.

Runtime requirements:

- Install `requirements.txt` in the same Python environment that runs
  `stdborg.py`.
- Run `codex login` for that runtime user so `~/.codex/auth.json` contains
  ChatGPT OAuth credentials.

The integration depends on the published `llm-openai-via-codex` pip package for
Codex OAuth token borrowing and refresh. Betterborg only owns the Telegram
message conversion, admin gating, and streaming response handling.

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

## Admin-only quick prefixes

- `.c` and `.cm`: GPT-5.6 Sol with `medium` reasoning.
- `.cl`: GPT-5.6 Sol with `low` reasoning.
- `.ch`: GPT-5.6 Sol with `high` reasoning.
- `.cx`: GPT-5.6 Sol with `xhigh` reasoning.
- `.cxx`: GPT-5.6 Sol with `max` reasoning.
- `.o`, `.om`, `.ol`, `.oh`, `.ox`, `.oxx`: the same ladder for GPT-6 Astra.

`.a` and `.s` are taken by other plugins, hence `.o` for Astra.

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
