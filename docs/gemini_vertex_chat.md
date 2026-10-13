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
  rather than silently falling back to AI Studio. The `-latest` aliases
  (including the bot's default model) and `gemini-3-pro-preview` exist only on AI
  Studio, so `/geminiProvider` warns when the user's model is one of them.
- **Attachments** are sent inline (base64). Vertex has no Files API, so
  `is_native_gemini_files_mode` is false for Vertex.
- **Explicit context caching is off.** Creating cached contents fails with
  "API keys are not supported by this API", so `cache_control` is stripped.
  Vertex still applies its own implicit caching.
- **Titles** of files and topics: a Gemini title model that Vertex lacks (the
  default is Flash Lite Latest) is replaced by
  `gemini_providers.VERTEX_TITLE_MODEL`, with the Vertex key.

## Still on Google AI Studio

Image generation (`.i` and the image models), voice replies (TTS), live mode
and the Gemini key rotation all keep using the AI Studio key whatever the user's
provider is.
