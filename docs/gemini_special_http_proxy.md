# Gemini Special HTTP Proxy

This document describes how outbound Google/Gemini API traffic is routed through a
dedicated HTTP proxy, controlled by `GEMINI_SPECIAL_HTTP_PROXY`.

## Summary

- When `GEMINI_SPECIAL_HTTP_PROXY` is set, **all** Gemini (Google API) traffic is routed
  through that proxy.
- Only the native `gemini/` provider (Google's API) is proxied. Non-Gemini providers
  (OpenRouter — including `google/*` — OpenAI, Codex, DeepSeek, Mistral) are **not** affected.
- By default the proxy is **admin-only** (`GEMINI_SPECIAL_HTTP_PROXY_ADMIN_ONLY_P=y`). A
  non-admin request raises `ProxyRestrictedException` with a user-facing message.

## Flags and Constants

- `GEMINI_SPECIAL_HTTP_PROXY`: the proxy URL (e.g. `http://host:port`,
  `socks5://host:port`). If unset, no proxying happens anywhere.
- `GEMINI_SPECIAL_HTTP_PROXY_ADMIN_ONLY_P`: defaults to `y`; restricts proxy use to admins.

Both live in `uniborg/llm_util.py`.

## How it works

There are two distinct Gemini API paths, and each has its own proxy mechanism.

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

## Access Control

`get_proxy_config_or_error(user_id)`:

- Returns `(None, None)` when `GEMINI_SPECIAL_HTTP_PROXY` is unset (no proxy).
- When the proxy is set and `GEMINI_SPECIAL_HTTP_PROXY_ADMIN_ONLY_P` is on, raises
  `ProxyRestrictedException` for non-admin users (admin check via `util.is_admin_by_id`).
- Otherwise returns `(proxy_url, None)`.

## Out of scope

Direct `httpx` downloads (e.g. audio-URL fetching, URL mimetype checks) are not Google API
calls and are not routed through this proxy.
