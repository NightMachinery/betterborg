# Google Antigravity ACP feasibility

Betterborg could act as an ACP client for Google’s official `antigravity-acp`
registry binary. Its native `oauth-personal` sign-in uses the signed-in user’s
Antigravity plan allowance. Google describes this integration for single-user,
interactive development. A Betterborg pilot would bind one Telegram user to
one isolated agent environment and let the agent manage its own Google login.
[Google’s ACP guidance](https://discuss.ai.google.dev/t/conflicting-guidance-on-using-t3-code-with-personal-google-ai-subscriptions/197131/3)
[Antigravity plans](https://antigravity.google/docs/plans)

The checksum-verified macOS registry binary (1.5.0) completed the ACP handshake
and advertised `oauth-personal`. It offered no terminal sign-in method even
with the client capability enabled. The separate `agy acp` CLI (1.5.12)
advertised `terminal-login`. The first pilot check is whether the registry
binary can hand off its browser sign-in from a headless Linux worker to the
Telegram user. Browser consent and a subscription-funded turn remain untested.
[Registry manifest](https://raw.githubusercontent.com/agentclientprotocol/registry/main/antigravity-acp/agent.json)
[Google’s CLI ACP authentication guide](https://antigravity.google/docs/ide/extensions/acp#authentication)

The registry agent publishes per-turn token breakdowns, including cached
tokens, and surfaces quota exhaustion. Google documents the remaining plan
allowance in its `/usage` CLI panel. A machine-readable
remaining allowance through the registry agent is unverified.
[Registry agent changelog](https://antigravity.google/docs/changelog?tab=hub)
[Model quotas](https://antigravity.google/docs/cli/commands/usage/)
