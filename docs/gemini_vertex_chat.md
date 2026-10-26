# Gemini Models on Vertex AI (LLM Chat)

Each user of the LLM chat bot chooses where native Gemini models (`gemini/...`)
run, with `/geminiProvider` in a private chat:

- **Google AI Studio** (the default): the Gemini Developer API key from
  `/setGeminiKey`. Every Gemini model is available.
- **Vertex AI**: an Express-mode API key from `/setVertexKey`. No project, region
  or service account is needed. The key is stored as the `vertex` service in the
  shared key database, so a key saved in the STT bot works here too.

`/geminiProvider aistudio` or `/geminiProvider vertex` switches without the menu.
Switching to a provider without a key starts its key setup. The choice is the
`gemini_provider` field of the user's preferences, and `/status` shows it.

Shared constants (provider ids, labels, the Vertex endpoint and the models it
serves) are in `uniborg/gemini_providers.py`; the STT bot uses them too.

## How requests reach Vertex AI

Vertex Express takes the same `generateContent` request body as AI Studio, at
`https://aiplatform.googleapis.com/v1/publishers/google/models/<model>:<action>`
with the key in `x-goog-api-key`. litellm's own `vertex_ai/` provider needs a
project and region, so the bot keeps the `gemini/` model ids and passes
`api_base=gemini_providers.VERTEX_API_BASE` to litellm instead. Checked on
2026-10-12 with streaming, reasoning effort, Google Search and URL context tools,
JSON mode, safety settings, an inline image and structured output.

Routing happens once per request, in `_gemini_chat_service`: a native Gemini chat
request of a user on Vertex gets the `vertex` service and key. The chat handler
and guest mode then thread `gemini_provider` through `build_conversation_history`
and `GenerationRequest`.

## Differences on Vertex AI

- **Models.** Only the models in `gemini_providers.VERTEX_CHAT_MODELS` answer.
  Any other Gemini model is refused with a message naming the Vertex models,
  rather than silently falling back to AI Studio. `gemini-3-pro-preview` exists
  only on AI Studio, so `/geminiProvider` warns when it is the user's model.
- **The `-latest` aliases** (including the bot's default model) are not served by
  Vertex either. Instead, the bot sends Flash (Latest) and Flash Lite (Latest)
  as the newest Flash and Flash Lite that Vertex serves, kept in
  `gemini_providers.VERTEX_LATEST_IDS`. See "Following new Flash releases"
  below. The STT bot sends them the same way.
- **Attachments** are sent inline (base64). Vertex has no Files API, so
  `is_native_gemini_files_mode` is false for Vertex.
- **Explicit context caching is off.** Creating cached contents fails with
  "API keys are not supported by this API", so `cache_control` is stripped.
  Vertex still applies its own implicit caching.
- **Titles** of files and topics use the Vertex key and Vertex ids, so the
  default Flash Lite Latest runs as the newest Vertex Flash Lite. A Gemini title model that
  Vertex lacks is replaced by `gemini_providers.VERTEX_TITLE_MODEL`.
- **Voice replies (TTS)** use the Vertex key and client
  (`genai.Client(vertexai=True, api_key=...)`). Vertex names the TTS models
  without "preview": `gemini_providers.VERTEX_MODEL_IDS` maps
  `gemini-2.5-flash-preview-tts` and `gemini-2.5-pro-preview-tts` to
  `gemini-2.5-flash-tts` and `gemini-2.5-pro-tts`. They run at temperature 0.3
  (`tts_util.VERTEX_TTS_TEMPERATURE`): at the bot's usual 1, the styled prompt
  made Vertex's Flash TTS read three words for 30 seconds to 9 minutes in 3 of 8
  tries, and at 0.3 none of 8 did (2026-10-16). Whether AI Studio's preview
  models do the same was not checked.
- **Native image generation** (a Gemini image model chosen as the chat model;
  `.i` always uses Codex) runs on Vertex the same way, with
  `gemini-2.5-flash-image-preview` mapped to `gemini-2.5-flash-image`. The
  Gemini 2.0 image models are not on Vertex and are refused.

## Following new Flash releases

`uniborg/vertex_latest.py` keeps `VERTEX_LATEST_IDS` current, in both the chat
and the STT bot.

- **Probing.** Whether an Express key may list Vertex's models was not checked,
  while `countTokens` is known to work with one (the STT key check uses it). So
  the module asks `countTokens` (free) about each id newer than the known
  newest one, through the next major version's .9, both as a release and as a
  `-preview`: from 3.8 Flash, `gemini-3.9-flash`, `gemini-3.9-flash-preview`,
  `gemini-4-flash`, `gemini-4.0-flash` and so on to `gemini-4.9-flash-preview`.
  A 200 means Vertex serves the model and a 404 means it does not. The newest
  served version wins, and its release beats its preview. Probing also starts
  from the hand-checked baselines (`VERTEX_FLASH_BASELINE`, 3.8 Flash, and
  `VERTEX_FLASH_LITE_BASELINE`, 3.5 Flash Lite, checked 2026-10-16), so a model
  Vertex withdraws falls back to the newest one it still serves.
- **Not followed:** ids outside the `gemini-X.Y-flash[-lite][-preview]`
  pattern, such as a dated `-preview-09-2025` release.
- **Cache.** The answer is saved in `~/.borg/vertex_latest_models.json` for 24
  hours. Each bot process sleeps until the cache expires, re-reads it (another
  process may have renewed it) and probes again if it is still stale.
- **Key.** Probes use the first bot admin's saved Vertex key
  (`/setVertexKey`). Without one, or when any probe fails other than with a
  404 (rate limit, network), the bot keeps what it has, writes nothing to the
  cache and tries again in an hour. Models found by an incomplete probe are
  still used until then.

## Still on Google AI Studio: live mode

Live mode (`/live`) keeps using the AI Studio key. Vertex's Live API rejects the
model name the `genai` SDK sends with an Express key
(`publishers/google/models/...`: "Invalid resource field value"). It accepts a
full `projects/<number>/locations/<region>/...` name, and then only for
`gemini-live-2.5-flash` (the native-audio models were not available), but an
Express key does not reveal its project number: the only way to learn it is from
the text of an unrelated error. That is too fragile to build on.
