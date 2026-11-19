# Google Antigravity in Betterborg

The private pilot runs the official Google Antigravity ACP agent (1.5.0).
Each approved bot admin signs in with their personal Google account and uses
that account's Antigravity plan allowance. Gemini chat uses the models Google
exposes to that account. Hydrogen, Neon, and Carbon are in `/setModel`.
[Google's ACP guidance](https://discuss.ai.google.dev/t/conflicting-guidance-on-using-t3-code-with-personal-google-ai-subscriptions/197131/3)
[Google's model availability](https://antigravity.google/docs/models)

1. In the Chat bot's DM, choose **Google Antigravity** in `/geminiProvider` or send
   `/antigravity_login`.
2. Open **Sign in with Google**. After consent, copy the full browser address
   beginning with `http://127.0.0.1:` and paste it back into the DM. The page
   may show a connection error. The bot forwards the address to the waiting
   agent. The sign-in message confirms the connection and Chat selects
   Antigravity on success.
3. Send a message in Chat, or select **Google Antigravity** in `/provider` in
   Transcribe or Imagine. Those bots use the same Google login and keep their
   own provider selection.

Use `/antigravity_usage` in Chat to view the allowance Google reports. This
queries Google's quota endpoint without a model turn.

- **Chat** uses the model you select. Send `.i` followed by a prompt to
  request an image through Antigravity.
- **Transcribe** uses Gemini 3.8 Flash for audio, the sound from video,
  and OCR for JPEG, PNG, and WebP. The total inline media limit is 12 MiB.
- **Imagine** uses Antigravity's native image tool. Number and aspect ratio
  are passed as prompt instructions. The agent returns images inline through ACP.

Betterborg stores Google's reusable grant in a private profile for the signed-in
Telegram account. `/antigravity_logout` removes the local grant and switches
Chat back to AI Studio. The other bots will ask you to reconnect when you
next use Antigravity. You can also revoke access in your Google account
settings. AI Studio and Vertex can still be selected in `/geminiProvider`.
Live mode uses AI Studio. Automatic voice replies
and LLM-generated titles are skipped for Antigravity turns.

## Worker operation

The three bots share one worker slot. If it is busy, Transcribe shows that it
is waiting and starts automatically when a slot becomes available. Use `/stop`
to cancel a waiting transcription. The wait expires after 15 minutes.
Each operation uses a fresh sandbox with an ephemeral workspace and session
store. The worker gets the shared official distribution and only the signing-in
user's persistent profile. Its private network has only loopback and can make
HTTPS CONNECT requests through a proxy for a fixed set of Google hosts.
The host side rejects private and loopback destinations. The bot relays the
authorization response to the original agent inside that same worker and
discards it from the chat history used for generation.

Chat's Google Search toggle in `/tools` controls `search_web` on text turns.
Image requests permit `generate_image`. Transcription requests have no tools.
The ACP client offers no filesystem or terminal capabilities and
provides no MCP servers. Shared API keys and the service's fallback routes do
not apply to Antigravity requests.

### Systemd service

Set up Betterborg from a reviewed checkout on a Linux x86-64 VM with systemd,
Landlock ABI 3 or newer, Python 3.11 or newer, and `curl`. From the
Betterborg checkout, run the installer as root and pass the Unix account
that runs the three bots.

```sh
sudo /bin/sh deployment/antigravity/install.sh BOT_UNIX_USER
```

On a fresh VM, the installer downloads the pinned ACP release from Google
and verifies the archive and extracted binaries. It reuses matching binaries
from an earlier systemd or Docker setup. On a VM without access to Google's
download server, pass the path to the official ZIP as the second argument.
The same SHA-256 checks apply. [ACP registry manifest](https://raw.githubusercontent.com/agentclientprotocol/registry/main/antigravity-acp/agent.json)

The installer creates the dedicated worker account, starts its Unix socket,
and checks that the pinned agent initializes from the bot account. Use the
same checkout command after a worker upgrade. [Deployment checks](deployment.md)

The systemd worker uses a private network namespace, a read-only system,
bounded temporary storage, a syscall filter, and CPU, RAM and process
limits. The trusted wrapper launches the native agent under a Landlock
allowlist for the current Google profile. OAuth returns enter the same
worker that initiated the sign-in. The Google proxy remains in Betterborg.

The installer enables systemd selection for bot revisions that support it.
If the installed service is unavailable, requests fail closed. Docker is
the backend on hosts without the installer configuration. For a rollback,
remove `/etc/betterborg-antigravity/enabled` as root and restart the three
bots together.

During an initial switch, sign in once in the Chat bot to create a shared
systemd profile. The service keeps it under its own Unix account. The
existing Docker profile remains in its original location and can be used
again on rollback.

### Docker backend

Install `agy_acp_server.par` and `localharness_external` from the Linux x86-64
release in `~/.local/lib/betterborg/antigravity-acp/1.5.0/`. Verify the
registry's SHA-256 before extracting and keep both files executable. Pull the
pinned Python image as the bot operator.

```sh
docker pull python@sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b
```

### Worker startup benchmark

Median across nine interleaved launches per configuration on Linux x86-64.
The Antigravity 1.5.0 package and Docker image were warm. Each run started with
a disposable profile and ended after the ACP initialization response. Memory
is proportional set size (PSS), which splits shared memory across the
processes that map it.

- Native launch under an unprivileged UID took 3.85 s, 3.80 CPU seconds, and
  297 MiB of PSS. This measures the agent after starting under the UID.
- A transient systemd service with Landlock filesystem restrictions, TCP
  sockets blocked, and cgroup limits took 3.95 s, 3.82 CPU seconds, and
  297 MiB of PSS.
- The deployed Docker launcher took 4.64 s, 4.30 CPU seconds, and 332 MiB of
  PSS.

Measurements include the worker and launcher processes at
initialization. Shared daemons, root-owned runtime helpers, and peak memory
during authenticated model turns fall outside these measurements. Antigravity
itself accounted for about 281 MiB in each configuration.

The lightweight benchmark used a transient user-level service and
blocked all IP sockets. The production unit adds a separate Unix account
and private network, with the same restricted Google proxy used by Docker.
[systemd sandbox options](https://www.freedesktop.org/software/systemd/man/latest/systemd.exec.html)

Before expanding beyond the pilot, measure peak memory and profile growth
and verify each target workflow across restart and unlink. Google documents personal OAuth for ACP
clients. A wider hosted rollout needs its own review of the service terms.
[Google's personal terms](https://antigravity.google/terms)
