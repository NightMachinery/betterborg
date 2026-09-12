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
  codex_users: [],
}
```

The two policy arrays accept numeric Telegram user IDs and the exact string
`"MAGIC_ADMINS"`. The sentinel delegates to Betterborg's existing
`util.isAdmin(event)` check, including trusted-chat access. Remove it to remove
that automatic grant. Numeric IDs remain supported for older configurations.

Use `codex_users` for a permanent roster with names and independently enabled
personal grants. For example:

```json5
codex_users: [
  {
    id: 123456789,
    name: "Example User",
    codex_enabled: true,
    imagegen_enabled: false,
  },
],
```

Each roster entry requires an integer `id` and both boolean fields; `name` is
optional. IDs must be unique within the roster. A roster entry replaces any
numeric grant for the same ID in the older arrays. **A disabled personal grant
does not override `MAGIC_ADMINS` or trusted-chat access.** Image generation
requires both effective Codex access and effective image access.

Both policy-array keys are required; the roster is optional for compatibility.
Empty policy arrays and no enabled roster grants allow nobody. Betterborg
reloads the file when it changes. Invalid or unreadable config disables all
Codex access until corrected, logs a diagnostic, and leaves the file untouched.
Other providers continue working. These grants do not confer bot-admin or
group-settings privileges.

### Managing access and defaults

Bot admins can use `.codex-users` to browse configured non-admin users. Disabled
users stay in the menu. Legacy numeric IDs from either policy array are also
included, but `MAGIC_ADMINS` itself does not add users. The list shows names,
IDs, personal access flags, and saved default models. Configured names take
precedence over Telegram names; the ID is the fallback if neither is available.

- `.codex-users`: browse the list and select a user.
- `.codex-users <user-id>`: inspect access and the saved default, toggle the
  personal Codex or image grant, or open the model picker.
- `.codex-users <user-id> <model-id>`: immediately save a specific model as
  that user's personal default, including custom model IDs.

Access buttons save an explicit enabled/disabled value to the JSON5 file and
refresh the menu. Repeated clicks do not invert the state unexpectedly.
Disabling Codex preserves the image flag: images are paused outside contexts
that still grant Codex access. Enabling images never implicitly enables Codex.
Turning either personal grant off leaves the configured admin/trusted-chat
policies in effect. The menu states this distinction.

Config writes use a lock and atomic replacement, preserving file permissions
and unrelated settings. Existing comments and formatting are retained when
updating a flag. Invalid config, removed users, or detected concurrent manual
edits prevent an update rather than replacing the config with defaults.

Model changes do not require the user's confirmation. They replace only the
saved personal default; the user can subsequently change it again. An admin
can save a Codex default while personal access is off, ready for re-enabling.
Chat-specific models and per-message prefixes retain their usual precedence.

The command and every button check the caller's bot-admin access and the
target's current roster membership. Bot admins are excluded as targets, and
admin-only models cannot be assigned to non-admin users. Invalid config and
removed membership prevent changes from previously opened menus. The existing
trusted-chat rules apply to the caller.

### When access is disabled

If a saved personal or chat model is Codex but the current request has no
Codex access, ordinary messages fall back to the default Gemini Flash model.
The bot sends a short notice identifying the fallback and explaining that
saved model settings are unchanged. If the fallback needs an API key, its
usual setup prompt follows. Re-enabling access restores use of the saved
Codex model without rewriting preferences.

There is no fallback notice when trusted-chat or admin rules still grant
Codex access. Explicit Codex requests and `.i` receive an access denial rather
than falling back. The existing `.c` exception remains: without Codex access,
it uses its OpenRouter meaning. Requests already in flight retain their
authorization snapshot; toggles affect subsequent requests.

## Runtime requirements

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
