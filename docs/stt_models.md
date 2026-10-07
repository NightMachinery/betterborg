# STT Models

Which provider and Gemini model the STT bot (`stt_plugins/stt.py`) transcribes
with. Each user picks a provider with `/provider` and a model with `/model`.
Provider metadata and the Vertex transport are in `uniborg/stt_providers.py`;
the common model menu and AI Studio calls are in `uniborg/stt_models.py`.

The call runs with the caller's selected provider and API key, so
whether a model works can depend on the key. A guest mention (in a chat the
bot is not in) uses the settings of the person who mentioned it. Existing
users keep Google AI Studio and their saved model choice.

## Provider and key setup

In a private chat, `/provider` shows the selected provider, its model, and
whether each provider has a saved key. Tap a provider with a saved key to
switch immediately. Tap one without a key to open setup, follow **Get API
key**, then send the key. The bot checks it with `countTokens`, saves it, and
only then switches. A failed check or cancellation preserves the old key and
provider. A saved key can later expire or lose model access; validation does
not guarantee that every transcription model is available.

**Manage API keys**, `/setGeminiKey [key]`, and `/setVertexKey [key]` update
keys without changing the selected provider. Setup deletes the submitted
message before checking the key, warns if deletion fails, and never displays
key values. Group commands direct users to private setup; an inline key sent
in a group is deleted on a best-effort basis and is not stored.

Google AI Studio uses its Gemini Developer API key. Vertex AI uses an
**Express-mode API key**, created through Vertex AI Studio. No project id,
region, service-account file or OAuth setup is required by the bot. The
providers use separate quota and billing, so switching is always explicit.
The bot does not infer a provider from the key's prefix.

Each provider remembers its own model choice. The provider, key and model
list are captured when a transcription job is prepared; changing settings
does not reroute an already prepared job. Retries, Auto fallbacks and Vertex
transcript filename generation stay on that job's provider.

The AI Studio admin key-rotation behavior is preserved. Vertex always uses
the caller's saved Vertex key.

## Vertex AI models

Vertex offers Auto, 3.5 Flash, 3 Flash Preview, 2.5 Flash and 2.5 Flash Lite.
Its Auto order is 3.5 Flash, 3 Flash Preview, then 2.5 Flash Lite. AI Studio's
`latest` aliases and 3.5 Transcribe are not offered for Vertex Express mode.
An explicit model choice uses that model alone.

Vertex calls the global Express endpoint directly with `x-goog-api-key` in a
header. Requests contain inline media and the transcription JSON schema.
HTTP clients are created inside the existing Gemini proxy context, including
key validation and filename generation. Upstream error bodies are replaced
with safe status descriptions before reaching error messages or logs.

Official references: [Express-mode setup and endpoints](https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/start/express-mode/overview),
[Express-mode API methods](https://docs.cloud.google.com/gemini-enterprise-agent-platform/reference/express-mode/api-reference),
and [API-key client example](https://docs.cloud.google.com/vertex-ai/generative-ai/docs/samples/googlegenaisdk-vertexai-express-mode).

## Google AI Studio models

- **Auto**, the default: the fallthrough list `STT_MODELS` in
  `uniborg/constants.py`, which is `gemini/gemini-2.5-flash`, then
  `gemini/gemini-flash-lite-latest`. See "Auto" below.
- **Flash Latest** and **Flash Lite Latest**: Google's own aliases, which
  follow its newest Flash and Flash Lite (3.8 Flash and 3.5 Flash Lite on
  2026-10-01).
- **Versioned Flash models**: 3.8, 3.7, 3.6 and 3.5 Flash; 3.5 and 3.1 Flash
  Lite; 3 Flash Preview; 2.5 Flash and 2.5 Flash Lite.
- **3.5 Transcribe**, Google's dedicated speech-to-text model. See "The speech
  model" below.

Any choice other than Auto is used alone: transient errors are retried on
that model, but no other model is tried, and its last error is shown. When it
refuses the key (as 2.5 Flash does for new keys), the user is told to pick
another model.

Preferences are saved per user under `~/.borg/stt_preferences/`: `provider`
selects the endpoint and `provider_models` maps each provider to a model id
or `auto`. The legacy `model` field remains readable and is maintained for
AI Studio. A saved model that its provider's menu no longer offers counts as
Auto. API keys remain in the existing private API-key database.

Two words used below: a **media model** is a general Gemini model, which gets
the bot's transcription prompt and a JSON schema, and reads audio, video and
images. The **speech model** is 3.5 Transcribe.

## Models the installed llm-gemini lacks

Media models are called through the `llm` library and its llm-gemini plugin,
which registers a fixed list of model ids. The server's llm-gemini (0.28.2)
stops at 2.5 Flash plus the two aliases, and newer releases need a newer `llm`,
which the Codex plugin may not support. So `load_media_model` builds a menu
model that llm-gemini does not know from llm-gemini's own `AsyncGeminiPro`
class, with schema support on. The class's constructor is the same in 0.28.2 and
0.32.

## The speech model

Gemini 3.5 Transcribe (generally available since 2026-08-26) works with the
free AI Studio keys the bot already uses. On the free tier Google may use the
data to improve its products, and on 2026-10-01 the free tier allowed only 3
requests a minute for this model. It differs from the media models:

- It takes audio and nothing else. Voice notes (Ogg Opus) are sent as they
  are; other audio and the sound of videos are converted to mono Ogg Opus with
  ffmpeg. Images and silent videos are skipped, and the reply says how many.
  Only images or silent videos is an error before any call.
- It returns plain text: no speaker labels, emoji, translations or visual
  descriptions, which the media models' prompt asks for.
- It rejects a system instruction ("Developer instruction is not enabled for
  this model") and a JSON schema ("JSON mode is not enabled"), and ignores a
  text prompt.
- It answers generateContent with an `audioTranscription` part instead of a
  `text` part. llm-gemini reads only `text`, so through it the answer is empty.
  `stt_models.transcribe` therefore calls the REST API itself, with the key in
  the `x-goog-api-key` header. Its HTTP client is made during the request, so
  `GEMINI_SPECIAL_HTTP_PROXY` applies to it as it does to llm-gemini.
- Several files go in one request and come back as one text, with no
  separator. One request per file would separate them, but would spend one
  request of the 3-a-minute allowance per file.

Google documents speaker diarization, timestamps and language hints for this
model only through its newer Interactions API (`transcription_config`). The
generateContent API rejects that field, and on 2026-10-01 the Interactions API
rejected the documented diarization option, so the bot uses neither.

## Auto

### Transient errors

A rate limit, overload or timeout (`_is_retriable_stt_error`) is retried up to
`STT_RETRIES_PER_MODEL` times on each model, `STT_RETRY_SLEEP` seconds apart,
before the next model in the list. The status message says so as it happens.

### A model that refuses the key

Google closed Gemini 2.5 Flash to new API keys: for those it answers 404
"This model models/gemini-2.5-flash is no longer available to new users".
Retrying cannot help, so such a refusal (`_is_model_unavailable_error`) moves
to the next model at once, and the bot remembers it for that key:

- Redis key `borg:model_unavailable:<key hash>:<model>`, where the hash is the
  first 32 hex digits of the key's SHA-256 (`redis_util.api_key_hash`), never
  the key itself. Vertex hashes a provider-prefixed key to keep its cache
  separate from AI Studio while preserving existing AI Studio marks.
- It lasts `STT_MODEL_UNAVAILABLE_SECONDS` (30 days), and while it lasts that
  key skips the model without a call. Other keys still try it.
- When every model in the list has refused the key, all are tried again, so a
  stale mark cannot leave a key with nothing.

So keys that can still use 2.5 Flash keep it, and new keys use Flash Lite
Latest. When Google shuts 2.5 Flash off for everyone, every key falls through
the same way.

The transcript log (`~/.borg/stt/log/<user id>/`) records the model that
answered, not the first one tried.

## Not offered

Google Cloud's Chirp 3 was considered and left out. It refuses API keys: it
needs a Google Cloud project with billing and a service account. It has no
free tier ($0.016 a minute), its synchronous API takes at most 60 seconds of
audio, and its Persian support is a preview without speaker separation.
OpenRouter also resells it (`google/chirp-3`) at the same price, which would
need only an OpenRouter key.
