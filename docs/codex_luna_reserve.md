# Codex Luna Reserve

The ChatGPT account behind Codex carries a second allowance, the Luna Reserve,
on its own weekly meter. It stays usable after the regular plan allowance is
spent, and `llm_chat` now falls back to it automatically.

## The routing slug

The Reserve presents itself as `gpt-5.6-luna`, but that slug bills to the
regular allowance and fails the moment the plan is spent. Only `gpt-reserve`
reaches the reserve meter. The Codex CLI keeps the two apart explicitly, in
`codex-rs/tui/src/model_catalog.rs`:

    pub(crate) const LUNA_RESERVE_MODEL: &str = "gpt-reserve";
    pub(crate) const LUNA_MODEL: &str = "gpt-5.6-luna";

Betterborg exposes it as `openai-codex/gpt-reserve`
(`OPENAI_CODEX_LUNA_RESERVE` in `uniborg/constants.py`), displayed as
"Luna Reserve (Codex)".

This is why the Reserve is widely believed to be unreachable from third-party
clients: the Codex model catalog marks `gpt-reserve` with `visibility: hide`
even though `supported_in_api` is true, so any model listing that filters on
visibility drops it. Attempts that instead request `gpt-5.6-luna` get
`usage_limit_reached` however much reserve is left.

## Verified behaviour

Checked against the live backend while the regular allowance was exhausted:

- `gpt-reserve` answers normally; `gpt-5.6-luna`, `gpt-5.6-sol`,
  `gpt-5.6-terra` and `gpt-5.5` all return `usage_limit_reached`.
- Every reasoning level is accepted: `none`, `low`, `medium`, `high`, `xhigh`
  and `max`, plus omitting the reasoning parameter entirely.
- `web_search` and `image_generation` both work, so tools and `.i` need no
  special case.
- No request header is needed. The `x-openai-codex-luna-reserve` header seen in
  the CLI is sent only on the usage-status endpoint, to make the Reserve meter
  appear in the reply.

The Reserve is documented as limited to selected personal Plus and Pro
accounts, so it may be absent elsewhere. Nothing assumes it exists.

## Automatic fallback

When a Codex request fails with `usage_limit_reached`, `chat_handler` retries
the same request once against `openai-codex/gpt-reserve`: same messages, same
tools, same image callback. Reasoning effort is re-resolved for the Reserve
model, because effort is a per-model preference. On success the answer carries
a note saying it came from the Reserve.

The retry runs on every message rather than being remembered. It only happens
after a request has already failed, and the account can regain allowance at any
time, so a cached "reserve is spent" flag would buy nothing and could go stale.

It applies to every Codex request, including the explicit `.c` / `.ch` /
`.as` / `.i` prefixes: someone who asked for Codex still gets Codex, just on
the other meter. Requests that already target the Reserve are not retried, and
failures that are not usage limits are left alone.

## Direct use

- `.cr` / `.چر` selects the Reserve for one message at medium effort.
- It also appears in `/setModel` and `/setModelHere` for users with Codex
  access, like any other Codex model.

## Reading the meters

`codex_util.fetch_codex_usage()` reads
`https://chatgpt.com/backend-api/wham/usage` with the borrowed OAuth token and
the `x-openai-codex-luna-reserve: 1` header, which is what makes the Reserve
entry appear at all. It returns the regular window plus every additional meter,
each with its allowed flag, percentage used and reset time.

The Reserve is reported under `limit_name: "gpt-reserve"` with
`metered_feature: "base_model_inference"`. `CodexUsage.reserve()` returns
`None` on an account that has no Reserve, and every surface uses that to decide
whether to mention it at all rather than showing an empty meter.

The lookup never raises and returns `None` on any failure, so a status panel
still renders when it is unavailable. It is used only by status surfaces, never
on the request path.

## Open lead

The same endpoint also reports `limit_name: "GPT-5.3-Codex-Spark"` with
`metered_feature: "codex_bengalfox"`, allowed and unused. It does not appear in
the model catalog under that name, so its routing slug is unknown. Possibly a
third pool; not investigated.

## Related

- [Codex models](codex_models.md)
- [Codex quota fallback](codex_quota_fallback.md)
- [Reasoning effort](reasoning_effort.md)
