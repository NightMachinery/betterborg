# STT Models

Which Gemini model the STT bot (`stt_plugins/stt.py`) transcribes with. Each
user picks one with `/model`; the menu and the code that calls the models are
in `uniborg/stt_models.py`.

The call runs with the caller's own Gemini key (or a rotated shared key), so
whether a model works can depend on the key. A guest mention (in a chat the
bot is not in) uses the choice of the person who mentioned it.

## The menu

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

The choice is saved per user under `~/.borg/stt_preferences/`, as the model id
or `auto`. A saved model that the menu no longer offers counts as Auto.

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
  the key itself.
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
