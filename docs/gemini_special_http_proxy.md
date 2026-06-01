# Gemini Special HTTP Proxy

This document describes how outbound Google/Gemini API traffic is routed through a
dedicated HTTP proxy, controlled by `GEMINI_SPECIAL_HTTP_PROXY`.

## Summary

- When `GEMINI_SPECIAL_HTTP_PROXY` is set, **all** Gemini (Google API) traffic is routed
  through that proxy.
- Only the native `gemini/` provider (Google's API) is proxied. Non-Gemini providers
  (OpenRouter — including `google/*` — OpenAI, Codex, DeepSeek, Mistral) are **not** affected.
- By default the proxy is **allowed for all users** (`GEMINI_SPECIAL_HTTP_PROXY_ADMIN_ONLY_P=n`).
  Set it to `y` to restrict proxy use to admins; non-admin requests then raise
  `ProxyRestrictedException` with a user-facing message.

## Flags and Constants

- `GEMINI_SPECIAL_HTTP_PROXY`: the proxy URL (e.g. `http://host:port`,
  `socks5://host:port`). If unset, no proxying happens anywhere.
- `GEMINI_SPECIAL_HTTP_PROXY_ADMIN_ONLY_P`: defaults to `n` (all users). Set to `y` to
  restrict proxy use to admins.

Both live in `uniborg/llm_util.py`.

## How it works

There are three distinct Gemini API paths, and each has its own proxy mechanism.

### 1. `genai` SDK calls (TTS, image generation, native image gen, file uploads, live)

`llm_util.create_genai_client(api_key, user_id=..., proxy_p=True)` wires the proxy into the
`genai.Client` via `HttpOptions.client_args`/`async_client_args`. Call sites that hit Google
endpoints pass `proxy_p=True`.

### 2. `litellm.acompletion` calls (the main chat path and filename generation)

litellm does not accept a proxy directly. `llm_util.create_litellm_proxy_client(user_id)`
returns a litellm `AsyncHTTPHandler` whose internal `httpx.AsyncClient` is replaced with a
proxied one, then passes it via the `client=` kwarg. For `gemini/*` models litellm forwards
that client to its Gemini handler. The handler is added only when
`is_native_gemini(model)` (chat) or `model.startswith("gemini/")` (filename gen) is true.

> **Streaming requires litellm >= 1.82.0.** Older litellm (e.g. 1.80.11) had a bug where
> `CustomStreamWrapper.fetch_stream` overrode the per-call `client=` with the global
> `module_level_aclient`, silently bypassing the proxy on streaming requests (manifesting as a
> 403 from Google's edge for region-restricted server IPs). Upstream fixed this in
> [BerriAI/litellm#17148](https://github.com/BerriAI/litellm/issues/17148) (released in
> v1.82.0) by adding a `gemini_client` parameter to the Gemini `make_call`, so the user's
> client survives the override. `requirements.txt` pins `litellm>=1.82.0` for this reason.
> Non-streaming was never affected.

Both mechanisms reuse `get_proxy_config_or_error()` for access control, so the admin-only
check and `ProxyRestrictedException` behavior are identical across all Gemini call sites.

### 3. `llm` library calls (STT transcription)

The STT plugin (`stt_plugins/stt.py`) calls Gemini through Simon Willison's `llm` library
(`llm.get_async_model(...).prompt(...)`), whose `llm-gemini` backend builds
`httpx.AsyncClient()` with **no args** — so it ignores `GEMINI_SPECIAL_HTTP_PROXY` (a no-arg
httpx client only honors the standard `HTTPS_PROXY`/`ALL_PROXY` env vars). This previously
caused an `httpx.ReadError` from region-restricted server IPs.

`llm_util.install_llm_gemini_proxy_patch()` (called once at STT plugin load) patches httpx's
client constructors to inject `proxy=` from a contextvar. `stt.py` sets that contextvar
(`set_llm_gemini_proxy` / `reset_llm_gemini_proxy`) around the `model.prompt(...)` call, using
the proxy from `get_proxy_config_or_error()`. The contextvar is `None` everywhere else, so all
other httpx traffic in the process (Telegram, etc.) is unaffected — the patch is effectively
Gemini-only despite touching the shared `httpx` module.

## Access Control

`get_proxy_config_or_error(user_id)`:

- Returns `(None, None)` when `GEMINI_SPECIAL_HTTP_PROXY` is unset (no proxy).
- When the proxy is set and `GEMINI_SPECIAL_HTTP_PROXY_ADMIN_ONLY_P` is enabled (`y`), raises
  `ProxyRestrictedException` for non-admin users (admin check via `util.is_admin_by_id`).
- Otherwise returns `(proxy_url, None)`.

## Out of scope

Direct `httpx` downloads (e.g. audio-URL fetching, URL mimetype checks) are not Google API
calls and are not routed through this proxy.
