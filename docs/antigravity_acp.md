# Google Antigravity in Gemini chat

The private pilot runs the official Google Antigravity ACP agent (1.5.0).
Each approved bot admin signs in with their personal Google account and uses
that account's Antigravity plan allowance. Gemini chat uses the models Google
exposes to that account. Hydrogen, Neon, and Carbon are in `/setModel`.
[Google's ACP guidance](https://discuss.ai.google.dev/t/conflicting-guidance-on-using-t3-code-with-personal-google-ai-subscriptions/197131/3)
[Google's model availability](https://antigravity.google/docs/models)

1. In the bot's DM, choose **Google Antigravity** in `/geminiProvider` or send
   `/antigravity_login`.
2. Open **Sign in with Google**. After consent, copy the full browser address
   beginning with `http://127.0.0.1:` and paste it back into the DM. The page
   may show a connection error. The bot forwards the address to the waiting
   agent and selects Antigravity on success.
3. Send a text message. Use `/antigravity_usage` to see the allowance Google
   reports. This command queries Google's quota endpoint without a model turn.

Betterborg stores Google's reusable grant in a private profile for the signed-in
Telegram account. `/antigravity_logout` removes the local grant and switches
Gemini back to AI Studio. You can also revoke access in your Google account
settings. AI Studio and Vertex can still be selected in `/geminiProvider`.
Live mode uses AI Studio. Antigravity chat supports text. Automatic voice replies
and LLM-generated titles are skipped for Antigravity turns.

## Worker operation

The pilot runs one worker at a time. Each operation uses a fresh unprivileged,
read-only container with an ephemeral workspace and session store. The worker
mounts the shared official distribution and only the signing-in user's
persistent profile. It has no direct network interface and can make HTTPS
CONNECT requests through the loopback proxy for a fixed set of Google hosts.
The host side rejects private and loopback destinations. The bot relays the
authorization response to the original agent inside that same container and
discards it from the chat history used for generation.

The existing Google Search toggle in `/tools` applies. The agent's built-in
allowlist contains only `search_web` when enabled. Only that permission can
be granted. The ACP client offers no filesystem or terminal capabilities and
provides no MCP servers. Shared API keys and the service's fallback routes do
not apply to Antigravity requests.

On Linux x86-64, install both `agy_acp_server.par` and
`localharness_external` from the release archive in
`~/.local/lib/betterborg/antigravity-acp/1.5.0/`. Verify the registry's SHA-256
before extracting and keep both files executable. Pull the pinned Python
container image as the bot operator. The worker launch uses that digest.
[ACP registry manifest](https://raw.githubusercontent.com/agentclientprotocol/registry/main/antigravity-acp/agent.json)

```sh
docker pull python@sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b
```

Before expanding beyond the pilot, exercise a real account through login,
a model turn, Google Search, quota read, restart and unlink, and measure
peak memory and profile growth. Google documents personal OAuth for ACP
clients. A wider hosted rollout needs its own review of the service terms.
[Google's personal terms](https://antigravity.google/terms)
