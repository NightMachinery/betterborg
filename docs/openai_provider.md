# OpenAI Provider

`llm_chat` offers each GPT family once: **GPT Sol**, **GPT Astra** and the
hidden **GPT Luna**, stored as the provider-neutral ids
`openai/gpt-sol-latest`, `openai/gpt-astra-latest` and `openai/gpt-luna-latest`.
Each user's **OpenAI provider** decides which backend answers a request. Saved
models and per-model effort therefore survive a provider switch.

Code: `uniborg/openai_providers.py` (providers, routing, quota memory) and the
request path in `llm_chat_plugins/llm_chat.py` (`_route_request_model`).

## Providers

`/openaiProvider` shows a panel; `/openaiProvider <name>` switches directly.

- `openai` (the default): the user's OpenAI API key, set with `/setOpenAIKey`.
  Requests use the public Responses API through the same streaming code as
  Codex (`codex_util.stream_codex_response` with `api_key`), so web search and
  reasoning effort behave the same. When every OpenAI key the user may use is
  out of credit and they may use Codex, that message is retried on Codex. Unlike
  `auto-openai-first`, the next message tries the keys again.
- `codex`: the shared ChatGPT subscription, under the usual Codex access rules
  ([codex_models.md](codex_models.md)).
- `openrouter`: OpenRouter's router aliases `~openai/gpt-*-latest`, with the
  user's OpenRouter key. OpenRouter takes only low/medium/high effort, so
  `none` is sent as low and `xhigh`/`max` as high.
- `auto-openai-first`: the OpenAI key first. When a key runs out of credit, the
  same message is retried on Codex and the key is skipped for 72 hours.
- `auto-codex-first`: Codex first. When the Codex account hits its usage limit,
  the same message is retried on the OpenAI key, and Codex is skipped until the
  reset its error names (one hour when it names none).

A user on `openai` who has no OpenAI key yet is answered by Codex if they may
use it, else by OpenRouter if they have an OpenRouter key. Nobody with a
working backend is stranded behind a key prompt; with neither, the key prompt
appears. The answer carries a note pointing at `/setOpenAIKey` only when the
user chose `openai` themselves: a user still on the default (most Codex users,
whose saved Codex models migrated) is not reminded under every answer.

The OpenAI API is sent the slug the Codex alias of the family points at now
(`codex_aliases`). The two catalogs name models alike; only Codex's is read.

## Personal ChatGPT plan usage

The `codex` provider uses one account for all authorized users. For users who
want to pay for their own requests today, `/setOpenAIKey` and
`/openaiProvider openai` use OpenAI API billing. See the provider and key
fallback rules above.

The first option to test for a shared bot is native Codex sign-in with one
isolated [Codex App Server](https://learn.chatgpt.com/docs/app-server#authentication)
worker per linked Telegram user. A disposable worker obtained and cancelled a
ChatGPT device code without an app registration. Two local workers also
reported distinct account state from separate `CODEX_HOME` directories. A live
pilot still needs to verify sign-in, subscription usage, and `.i` delivery for
two linked users on the same bot.

OpenAI offers **Sign in with ChatGPT** with a separate permission to spend an
eligible user's ChatGPT plan allowance. A remotely hosted Betterborg instance
needs to [request hosted access](https://openai.com/form/sign-in-with-chatgpt-interest/)
before offering that permission. The [open-source integration guide](https://developers.openai.com/siwc/token-sharing-open-source)
covers local clients and personal installations.

A personal Telegram bot is another deployment option for users who can run
their own process. A Betterborg runner could connect directly to Telegram and
use a local [Codex App Server](https://learn.chatgpt.com/docs/app-server#authentication)
with Codex-managed ChatGPT sign-in. The user would keep their credentials and
run the process on their own machine. Codex's [built-in image generation](https://learn.chatgpt.com/docs/image-generation)
uses the account's Codex allowance. A personal runner would need to verify
delivery of App Server image results to Telegram.

For a hosted integration, start with a **Connect ChatGPT** button in the user's
private bot chat. Bind the completed browser authorization to the initiating
Telegram user ID. Route eligible chat requests through the existing Responses
stream with that user's OAuth credential, using a distinct provider selection.
Keep quota and reconnect state scoped to the linked account, and require an
explicit choice before a request can use another billing source.
[OpenAI's inference guide](https://developers.openai.com/siwc/token-sharing-open-source/models-and-inference)
documents the Responses route for ChatGPT plan usage.

Start with chat. The [documented preview](https://developers.openai.com/siwc/token-sharing-open-source/preview-limitations)
excludes image generation, including through the Codex App Server plan-usage
configuration. For user-funded `.i` requests, a separate option would call the
[Images API](https://developers.openai.com/api/docs/guides/image-generation)
with the user's OpenAI project key. Eligible Pro and Business subscribers can
[claim API credits](https://developers.openai.com/api/docs/guides/chatgpt-api-credits)
that cover image generation and editing in the linked API organization.
If Betterborg adds coding sessions with workspace tools, use the
[Codex App Server integration](https://developers.openai.com/siwc/token-sharing-open-source/codex-app-server).

## Quota memory

`openai_providers.QuotaMemory` records two facts in Redis, so every process and
restart agrees (a process without Redis remembers in memory):

- An OpenAI key that answered `insufficient_quota` (no credit left, not an
  ordinary rate limit), keyed by a hash of the key, for 72 hours. Everyone who
  uses that key, directly or shared, skips it together.
- That the Codex account is over its usage limit, until its reset. The Luna
  Reserve has its own meter and does not count.

Only the auto providers consult the memory when choosing a backend. If every
OpenAI key is marked spent and Codex is spent too, the key is tried anyway: the
credit may have been topped up. The panel's **Retry OpenAI now** button
forgets the user's spent keys at once.

A request moves to the other backend only when nothing has been streamed yet,
and the answer carries a note saying so. Codex and the OpenAI API take the same
history (text and images), so only the backend changes.

Both request paths, chat messages and guest answers (mentions in other chats),
fail over through one helper, `_generate_with_failover` in `llm_chat`. It moves
to the next key of the user's chain first (`key_sharing`), then to the other
backend. Guest answers used to start on the first key and stop there, so a
spent OpenAI key ended a guest answer with "OpenAI request failed" even on an
auto provider; only the next message, which found the key marked spent, went
to Codex. A chat answer streamed as a Telegram draft keeps streaming as a draft
across a retry, and its Stop button cancels whichever attempt is running.

## What stays on Codex

- The Luna Reserve (`.cr`), a Codex meter.
- Image generation (`.i`), a Codex tool. A GPT family selected with `.i` runs
  on its Codex model.
- Title models: a GPT family chosen as title model writes titles on Codex, or on
  the free default without Codex access, never on a paid key.

## Audio

None of the backends hear audio. OpenRouter's catalog, which mirrors OpenAI's,
lists Sol, Astra and Luna with text, image and file input only (checked
2026-11-01); only `gpt-audio` models take audio. Audio links therefore keep
going to Gemini Flash ([codex_models.md](codex_models.md) and the audio URL
fallback in `llm_chat`).

## Migration

Settings saved with the Codex aliases (`openai-codex/gpt-*-latest`, and every
older Codex id) load as the neutral ids, effort included, and so follow the
provider default `openai`. Settings saved with OpenRouter's GPT Sol keep naming
OpenRouter explicitly; the picker no longer offers that entry.

## Shortcuts

`.c*` (Sol) and `.as*` (Astra), with their Persian aliases, select the GPT
families for everyone and run on the provider. `.cr` stays Codex-only.
